"""Bridge native lifecycle hooks to an armed Agent close request."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from host import Host


def state_root() -> Path:
    return (
        Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state")
        / "close-lifecycle"
    )


def _payload() -> dict[str, Any]:
    try:
        value = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _identity(data: dict[str, Any]) -> tuple[str | None, str | None]:
    return _text(data.get("session_id") or data.get("sessionId")), _text(
        data.get("turn_id") or data.get("turnId")
    )


def _state_path(session_id: str) -> Path:
    if any(part in session_id for part in ("/", "\\", "..")):
        raise ValueError("invalid session id")
    return state_root() / (session_id + ".json")


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, sort_keys=True) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _armed(state: dict[str, Any]) -> bool:
    return (
        isinstance(state.get("completion"), str)
        and isinstance(state.get("cancel"), str)
        and isinstance(state.get("completion_token"), str)
    )


def _signal(path: str, value: dict[str, Any]) -> None:
    _write(Path(path), value)


def record_assessment(state: dict[str, Any]) -> None:
    path = state_root() / "assessments" / _state_path(state["session_id"]).name
    _write(path, state)


def _assessment_lock(session_id: str) -> IO[bytes]:
    from runtime_delivery import acquire_lock

    acquire: Callable[[Path], IO[bytes] | None] = acquire_lock
    path = (
        state_root() / "assessments" / _state_path(session_id).with_suffix(".lock").name
    )
    deadline = time.monotonic() + 2
    while (lock := acquire(path)) is None:
        if time.monotonic() >= deadline:
            raise TimeoutError("close assessment stayed locked")
        time.sleep(0.01)
    return lock


def _owns_assessment(assessment: dict[str, Any]) -> bool:
    current = _read(
        state_root() / "assessments" / _state_path(assessment["session_id"]).name
    )
    return current is not None and current.get(
        "assessment_generation"
    ) == assessment.get("assessment_generation")


def _input_path(session_id: str) -> Path:
    return state_root() / "inputs" / _state_path(session_id).name


def remember_input(
    host: Host, session_id: str, turn_id: str | None, data: dict[str, Any]
) -> None:
    supplied = _text(data.get("transcript_path") or data.get("transcriptPath"))
    _write(
        _input_path(session_id),
        {
            "host": host.value,
            "session_id": session_id,
            "turn_id": turn_id,
            "profile": str(host.configuration_root.resolve()),
            "transcript_path": supplied,
            "input_generation": secrets.token_urlsafe(24),
        },
    )


def current_input(
    host: Host, session_id: str, supplied: Path | None = None
) -> dict[str, Any]:
    from host_sessions import SESSIONS

    state = _read(_input_path(session_id))
    if (
        not state
        or state.get("host") != host.value
        or state.get("session_id") != session_id
        or not isinstance(state.get("input_generation"), str)
        or state.get("profile") != str(host.configuration_root.resolve())
    ):
        raise ValueError(
            "native input hook receipt is unavailable; preserve the session"
        )
    recorded = state.get("transcript_path")
    if (
        supplied is not None
        and recorded
        and supplied.resolve() != Path(recorded).resolve()
    ):
        raise ValueError("transcript differs from the native input hook receipt")
    native = SESSIONS[host].close_target(
        session_id, supplied or (Path(recorded) if recorded else None)
    )
    if host is Host.CODEX and not _text(state.get("turn_id")):
        raise ValueError("native input hook receipt has no Codex turn")
    return {**state, **native}


def require_input(host: Host, session_id: str, state: dict[str, Any]) -> None:
    current = current_input(host, session_id)
    if any(state.get(key) != value for key, value in current.items()):
        raise ValueError("native input changed during the close transition")


def cancel(host: Host, session_id: str, reason: str) -> None:
    state = _read(_state_path(session_id))
    if (
        state
        and state.get("host") == host.value
        and state.get("session_id") == session_id
        and _armed(state)
    ):
        _signal(state["cancel"], {"cancelled": True, "reason": reason})


def _arm(
    host: Host,
    session_id: str,
    profile: Path,
    native: dict[str, Any],
    shared: Any,
    supplied: Path | None,
    assessment: dict[str, Any],
) -> dict[str, Any]:
    registry = state_root().parent / "cli-sessions" / (session_id + ".json")
    sessions = shared.host_sessions(host.value)
    record = shared.read_json(registry) or {}
    entry = sessions.prepared_record(record, session_id, shared)
    if (
        entry.get("host") != host.value
        or entry.get("session_id") != session_id
        or shared.path_key(entry.get("profile", "")) != shared.path_key(profile)
    ):
        raise ValueError(
            "native frontend does not match the requested session and profile"
        )
    turn = native.get("turn_id")
    armed = sessions.arm_close(entry, turn, native, supplied, shared)
    require_input(host, session_id, native)
    if shared.read_json(registry) != entry:
        raise ValueError("session registration changed before arming")
    root = state_root()
    canonical = _state_path(session_id)
    if canonical.exists():
        raise RuntimeError("a close request is already pending for this session")
    token = secrets.token_urlsafe(24)
    request = root / "requests" / (token + ".json")
    result = root / "requests" / (token + ".result.json")
    completion = root / "requests" / (token + ".complete.json")
    cancel = root / "requests" / (token + ".cancel.json")
    state = {
        **native,
        **armed,
        "mode": "close",
        "entry": entry,
        "assessment_generation": assessment["assessment_generation"],
        "armed_at": time.time(),
        "registry": str(registry.resolve()),
        "completion": str(completion),
        "cancel": str(cancel),
        "completion_token": token,
        "result": str(result),
    }
    raw = json.dumps(state, sort_keys=True).encode("utf-8") + b"\n"
    published = []
    try:
        with _assessment_lock(session_id):
            if not _owns_assessment(assessment):
                raise ValueError("a later close assessment superseded this attempt")
            for path in (request, canonical):
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    published.append((path, os.fstat(stream.fileno()).st_ino))
                    stream.write(raw)
            require_input(host, session_id, native)
            record_assessment(
                {**assessment, "status": "armed", "completion_token": token}
            )
        watcher = shared.start_agent_watcher(
            Path(shared.__file__).resolve().with_name("client_close_watcher.py"),
            [
                "--request",
                str(request),
                "--request-sha256",
                hashlib.sha256(raw).hexdigest(),
            ],
        )
    except Exception:
        for path, inode in reversed(published):
            if path.exists() and path.stat().st_ino == inode:
                path.unlink()
        raise
    return {
        "watcher_pid": watcher,
        "completion_token": token,
        "result": str(result),
        "request": str(request),
        "request_sha256": hashlib.sha256(raw).hexdigest(),
        "completion": str(completion),
        "cancel": str(cancel),
    }


def assess(
    host: Host,
    session_id: str,
    profile: Path,
    decision: str,
    reason: str,
    shared: Any,
    supplied: Path | None = None,
) -> dict[str, Any]:
    if decision != "restart" or not reason.strip():
        raise ValueError(
            "assessment needs an explicit restart decision and a nonempty reason"
        )
    native = current_input(host, session_id, supplied)
    if shared.path_key(native["profile"]) != shared.path_key(profile):
        raise ValueError("native input belongs to a different profile")
    assessment = {
        **native,
        "decision": decision,
        "reason": reason,
        "at": time.time(),
        "assessment_generation": secrets.token_hex(16),
        "status": "unavailable",
    }
    with _assessment_lock(session_id):
        require_input(host, session_id, native)
        if _state_path(session_id).exists():
            assessment["reason"] = "a close request is already pending for this session"
            previous = _read(
                state_root() / "assessments" / _state_path(session_id).name
            )
            if previous is None or any(
                previous.get(key) != value for key, value in native.items()
            ):
                record_assessment(assessment)
            return assessment
        record_assessment(assessment)
    try:
        assessment.update(
            _arm(host, session_id, profile, native, shared, supplied, assessment),
            status="armed",
        )
    except (OSError, RuntimeError, ValueError) as error:
        assessment["reason"] = str(error)
        with _assessment_lock(session_id):
            if _owns_assessment(assessment):
                record_assessment(assessment)
    return assessment


def run(event: str, host: Host, data: dict[str, Any]) -> int:
    from host_sessions import SESSIONS

    session_id, turn_id = _identity(data)
    if not session_id:
        return 0
    path = _state_path(session_id)
    state = _read(path) or {}
    owned = (
        isinstance(state, dict)
        and state.get("host") == host.value
        and state.get("session_id") == session_id
    )
    if event == "prompt":
        cancel(host, session_id, "new prompt")
        remember_input(host, session_id, turn_id, data)
        return 0
    if event == "stop":
        if _read(_input_path(session_id)) is None:
            remember_input(host, session_id, turn_id, data)
        if owned and _armed(state) and Path(state["cancel"]).exists():
            return 0
        if owned and _armed(state) and Path(state["completion"]).exists():
            _signal(state["cancel"], {"cancelled": True, "reason": "another stop"})
            return 0
        if owned and _armed(state) and SESSIONS[host].completes(state, data, turn_id):
            _signal(state["completion"], {"token": state["completion_token"]})
        return 0
    if event == "end":
        cancel(host, session_id, "session ended")
        _input_path(session_id).unlink(missing_ok=True)
        return 0
    raise ValueError("unknown lifecycle event")


def main(argv: list[str] | None = None) -> int:
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("event", choices=("prompt", "stop", "end"))
        parser.add_argument("--host", type=Host, choices=list(Host), required=True)
        args = parser.parse_args(argv)
        return run(args.event, args.host, _payload())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"close_lifecycle: {type(error).__name__}: {error}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
