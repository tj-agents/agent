"""Bridge native lifecycle hooks to an armed Agent close request."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from host import Host


def state_root() -> Path:
    return Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state") / "close-lifecycle"


def _payload() -> dict:
    try:
        value = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _identity(data: dict) -> tuple[str | None, str | None]:
    return _text(data.get("session_id") or data.get("sessionId")), _text(data.get("turn_id") or data.get("turnId"))


def _state_path(session_id: str) -> Path:
    if any(part in session_id for part in ("/", "\\", "..")):
        raise ValueError("invalid session id")
    return state_root() / (session_id + ".json")


def _read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _armed(state: dict) -> bool:
    return (isinstance(state.get("completion"), str) and isinstance(state.get("cancel"), str)
            and isinstance(state.get("completion_token"), str))


def _signal(path: str, value: dict) -> None:
    _write(Path(path), value)


def run(event: str, host: Host, data: dict) -> int:
    from host_sessions import SESSIONS

    session_id, turn_id = _identity(data)
    if not session_id:
        return 0
    path = _state_path(session_id)
    state = _read(path)
    owned = isinstance(state, dict) and state.get("host") == host.value and state.get("session_id") == session_id
    if event == "prompt":
        # A fresh prompt invalidates a previously armed final-reply completion.
        if owned:
            _signal(state["cancel"], {"cancelled": True, "reason": "new prompt"})
        return 0
    if event == "stop":
        if owned and Path(state["completion"]).exists():
            _signal(state["cancel"], {"cancelled": True, "reason": "another stop"})
            return 0
        # Stop is synchronous while the final reply's native turn is still identifiable.
        if owned and _armed(state) and SESSIONS[host].completes(state, data, turn_id):
            _signal(state["completion"], {"token": state["completion_token"]})
        return 0
    if event == "end":
        # Session end can never complete a later turn. Cancel this exact pending request instead.
        if owned:
            _signal(state.get("cancel", str(path) + ".cancel"), {"cancelled": True, "reason": "session ended"})
        return 0
    raise ValueError("unknown lifecycle event")


def main(argv: list[str] | None = None) -> int:
    try:
        from host import Host

        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("event", choices=("prompt", "stop", "end"))
        parser.add_argument("--host", type=Host, choices=list(Host), required=True)
        args = parser.parse_args(argv)
        return run(args.event, args.host, _payload())
    except Exception as error:
        print(f"close_lifecycle: {type(error).__name__}: {error}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
