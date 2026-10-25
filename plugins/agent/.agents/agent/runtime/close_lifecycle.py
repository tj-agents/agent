"""Bridge native lifecycle hooks to an armed Agent close request."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

TRANSCRIPT_TAIL_BYTES = 256 * 1024

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


def _matches(state: dict | None, host: str, session_id: str | None, turn_id: str | None) -> bool:
    return (isinstance(state, dict) and state.get("host") == host and state.get("session_id") == session_id
            and state.get("turn_id") == turn_id and isinstance(state.get("completion"), str)
            and isinstance(state.get("cancel"), str) and isinstance(state.get("completion_token"), str))


def _signal(path: str, value: dict) -> None:
    _write(Path(path), value)


def _records(path: Path) -> list[dict]:
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - TRANSCRIPT_TAIL_BYTES))
        lines = stream.read().splitlines()
    values = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _is_user(record: dict) -> bool:
    if record.get("type") != "user" or record.get("isMeta") or record.get("isCompactSummary"):
        return False
    content = (record.get("message") or {}).get("content")
    return isinstance(content, str) or (isinstance(content, list) and not any(
        isinstance(item, dict) and item.get("type") == "tool_result" for item in content))


def _claude_finished(state: dict, data: dict) -> bool:
    supplied = _text(data.get("transcript_path") or data.get("transcriptPath"))
    expected = _text(state.get("transcript"))
    token = _text(state.get("claude_user_uuid"))
    if not supplied or not expected or not token or Path(supplied).resolve() != Path(expected).resolve():
        return False
    records = _records(Path(expected))
    users = [record.get("uuid") for record in records if _is_user(record) and isinstance(record.get("uuid"), str)]
    if not users or users[-1] != token:
        return False
    after = False
    finished = False
    for record in records:
        if _is_user(record) and record.get("uuid") == token:
            after = True
            continue
        if after and _is_user(record):
            return False
        if after and record.get("type") == "assistant":
            message = record.get("message")
            finished = isinstance(message, dict) and message.get("stop_reason") == "end_turn"
    return finished


def run(event: str, host: str, data: dict) -> int:
    session_id, turn_id = _identity(data)
    if not session_id:
        return 0
    path = _state_path(session_id)
    state = _read(path)
    if event == "prompt":
        # A fresh prompt invalidates a previously armed final-reply completion.
        if isinstance(state, dict) and state.get("host") == host and state.get("session_id") == session_id:
            _signal(state["cancel"], {"cancelled": True, "reason": "new prompt"})
        return 0
    if event == "stop":
        if (isinstance(state, dict) and state.get("host") == host and state.get("session_id") == session_id
                and Path(state["completion"]).exists()):
            _signal(state["cancel"], {"cancelled": True, "reason": "another stop"})
            return 0
        # Stop is synchronous while the final reply's native turn is still identifiable.
        if host == "claude":
            if (isinstance(state, dict) and state.get("host") == host and state.get("session_id") == session_id
                    and _claude_finished(state, data)):
                _signal(state["completion"], {"token": state["completion_token"]})
            return 0
        if not turn_id:
            return 0
        if _matches(state, host, session_id, turn_id):
            _signal(state["completion"], {"token": state["completion_token"]})
        return 0
    if event == "end":
        # Session end can never complete a later turn. Cancel this exact pending request instead.
        if isinstance(state, dict) and state.get("host") == host and state.get("session_id") == session_id:
            _signal(state.get("cancel", str(path) + ".cancel"), {"cancelled": True, "reason": "session ended"})
        return 0
    raise ValueError("unknown lifecycle event")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", choices=("prompt", "stop", "end"))
    parser.add_argument("--host", choices=("claude", "codex"), required=True)
    args = parser.parse_args(argv)
    try:
        return run(args.event, args.host, _payload())
    except (OSError, ValueError):
        # Hooks must not block host lifecycle due to close bookkeeping.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
