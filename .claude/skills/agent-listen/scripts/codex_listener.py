#!/usr/bin/env python3
"""Start or inspect one detached Codex background listener."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid


SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def state_root() -> Path:
    return Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state")


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("listener state is invalid")
    return value


def session_record(session_id: str) -> dict:
    if SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("session id is invalid")
    path = state_root() / "cli-sessions" / (session_id + ".json")
    record = read_json(path)
    required = ("session_id", "host", "cwd", "profile", "recording_path", "client_message_id")
    if any(not isinstance(record.get(key), str) or not record[key] for key in required):
        raise ValueError("current Codex frontend binding is unavailable")
    if record["session_id"] != session_id or record["host"] != "codex":
        raise ValueError("current session does not have a Codex frontend binding")
    if not Path(record["cwd"]).is_dir() or not Path(record["profile"]).is_dir():
        raise ValueError("current Codex frontend paths are unavailable")
    return record


def session_close():
    here = Path(__file__).resolve()
    for root in (here.parent, *here.parents):
        candidate = root / ".agents" / "agent" / "utility" / "peer" / "scripts" / "session_close.py"
        if candidate.is_file():
            parent = str(candidate.parent)
            if parent not in sys.path:
                sys.path.insert(0, parent)
            spec = importlib.util.spec_from_file_location("codex_listener_session_close", candidate)
            module = importlib.util.module_from_spec(spec)
            if spec is None or spec.loader is None:
                break
            spec.loader.exec_module(module)
            return module
    raise RuntimeError("the packaged Agent detached watcher capability is unavailable")


def start(arguments: argparse.Namespace) -> int:
    session_id = arguments.session_id or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    if not session_id:
        raise ValueError("Codex thread identity is unavailable")
    command = arguments.command[1:] if arguments.command[:1] == ["--"] else arguments.command
    if not command:
        raise ValueError("listener command is required after --")
    if not 1 <= arguments.timeout <= 86400:
        raise ValueError("--timeout must be between 1 and 86400 seconds")
    record = session_record(session_id)
    listener_id = uuid.uuid4().hex
    path = state_root() / "listeners" / (listener_id + ".json")
    request = {
        "schema_version": 1,
        "listener_id": listener_id,
        "state": "starting",
        "created_at": time.time(),
        "session": record,
        "purpose": arguments.purpose,
        "timeout": arguments.timeout,
        "command": command,
    }
    write_json(path, request)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    watcher = Path(__file__).with_name("codex_listener_watcher.py")
    session_close().start_agent_watcher(watcher, ["--request", str(path), "--request-sha256", digest])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = read_json(path)
        if current.get("state") != "starting":
            break
        time.sleep(0.05)
    current = read_json(path)
    if current.get("state") != "running":
        raise RuntimeError("listener did not acknowledge startup")
    print(json.dumps({"status": "listening", "listener_id": listener_id, "state": str(path)}))
    return 0


def status(arguments: argparse.Namespace) -> int:
    if re.fullmatch(r"[0-9a-f]{32}", arguments.listener_id) is None:
        raise ValueError("listener id is invalid")
    print(json.dumps(read_json(state_root() / "listeners" / (arguments.listener_id + ".json")), sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    command = commands.add_parser("start")
    command.add_argument("--session-id")
    command.add_argument("--purpose", required=True)
    command.add_argument("--timeout", type=int, default=7200)
    command.add_argument("command", nargs=argparse.REMAINDER)
    query = commands.add_parser("status")
    query.add_argument("--listener-id", required=True)
    arguments = parser.parse_args(argv)
    try:
        return start(arguments) if arguments.operation == "start" else status(arguments)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "unavailable", "reason": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
