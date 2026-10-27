#!/usr/bin/env python3
"""Prepare or signal an exact CLI close without inferring a frontend identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sys
import re
import time


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def write_object(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def identity(value: object, label: str) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("pid"), int) or isinstance(value["pid"], bool) or value["pid"] <= 0:
        raise ValueError(f"{label} needs an exact positive pid")
    started = value.get("started_at")
    if (not isinstance(started, (int, float)) or isinstance(started, bool)
            or not math.isfinite(started) or started <= 0):
        raise ValueError(f"{label} needs an exact process creation time")
    return {"pid": value["pid"], "started_at": started}


def target_from_record(record: dict) -> dict:
    return {"schema_version": 1, "frontend": identity(
        {"pid": record.get("pid"), "started_at": record.get("pid_started_at")}, "frontend",
    ), "entry": record}


def state_root() -> Path:
    return Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state")


def cli_capability_root(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError("declared cli:close capability path is unavailable")
    for root in (resolved.parent, *resolved.parents):
        markers = (root / ".git", root / ".codex-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json")
        if not any(marker.exists() for marker in markers):
            continue
        identity = root / ".agents" / "cli" / "utility" / "close" / "scripts" / "process_identity.py"
        if identity.is_file():
            return root
        break
    raise ValueError("declared cli:close capability does not ship process_identity.py")


def record_path(session_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise ValueError("session id must be one registry filename component")
    return state_root() / "cli-sessions" / f"{session_id}.json"


def prepare(args: argparse.Namespace) -> int:
    record_path(args.session_id)
    record = read_object(args.record or record_path(args.session_id))
    shared = _session_close()
    try:
        sessions = shared.host_sessions(record.get("host"))
    except shared.Refusal:
        raise ValueError("record has no supported native host") from None
    record = sessions.prepared_record(record, args.session_id, shared)
    if (record.get("session_id") != args.session_id
            or shared.path_key(record.get("profile", "")) != shared.path_key(args.profile)):
        raise ValueError("record does not match the requested session and profile")
    target = target_from_record(record)
    target.update(sessions.close_target(args.session_id, args.transcript))
    write_object(args.target, target)
    print(json.dumps({"status": "prepared", "target": str(args.target)}))
    return 0


def arm(args: argparse.Namespace) -> int:
    registry = record_path(args.session_id)
    token = args.completion_token or secrets.token_urlsafe(24)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token):
        raise ValueError("completion token must be one safe filename component")
    state_path = state_root() / "close-lifecycle" / f"{args.session_id}.json"
    if args.state is not None and args.state.resolve() != state_path.resolve():
        raise ValueError("close state must use the canonical session path")
    if state_path.exists():
        raise RuntimeError("a close request is already armed for this session")
    cli_root = cli_capability_root(args.cli_close)
    shared = _session_close()
    shared.process_identity_runtime(cli_root)
    sessions = shared.host_sessions(args.host)
    target = read_object(args.target)
    if target.get("schema_version") != 1:
        raise ValueError("target schema is unavailable")
    frontend = identity(target.get("frontend"), "frontend")
    entry = target.get("entry")
    if (not isinstance(entry, dict) or entry.get("session_id") != args.session_id
            or entry.get("host") != args.host
            or shared.path_key(entry.get("profile", "")) != shared.path_key(args.profile)):
        raise ValueError("record does not match the requested session and profile")
    if target_from_record(entry)["frontend"] != frontend:
        raise ValueError("target does not match the attributable frontend")
    if read_object(args.record or registry) != entry:
        raise ValueError("prepared session registration changed before arming")
    armed = sessions.arm_close(entry, args.turn_id, target, args.transcript, shared)
    request = state_root() / "client-close" / (token + ".json")
    result = state_root() / "client-close" / (token + ".result.json")
    completion, cancel = args.completion.resolve(), args.cancel.resolve()
    paths = [request.resolve(), result.resolve(), state_path.resolve(), completion, cancel]
    if len(set(paths)) != len(paths) or any(path.exists() for path in paths):
        raise ValueError("close request paths must be distinct and unused")
    state = {
        "mode": "close", "entry": entry, "cli_root": str(cli_root), "armed_at": time.time(),
        "registry": str((args.record or registry).resolve()),
        "host": args.host, "session_id": args.session_id, "turn_id": args.turn_id,
        "completion": str(completion), "cancel": str(cancel), "completion_token": token,
        "result": str(result.resolve()),
        **armed,
    }
    raw = json.dumps(state, sort_keys=True).encode("utf-8") + b"\n"
    digest = hashlib.sha256(raw).hexdigest()
    published = []
    try:
        for path in (request, state_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                published.append((path, os.fstat(stream.fileno()).st_ino))
                stream.write(raw)
        watcher = shared.start_agent_watcher(
            Path(shared.__file__).resolve().with_name("client_close_watcher.py"),
            ["--request", str(request.resolve()), "--request-sha256", digest],
        )
    except Exception:
        for path, inode in reversed(published):
            if path.exists() and path.stat().st_ino == inode:
                path.unlink()
        raise
    print(json.dumps({"status": "armed", "completion_token": token, "watcher_pid": watcher,
                      "result": str(result)}))
    return 0


def _session_close():
    import importlib.util

    here = Path(__file__).resolve()
    locator = next((root / ".agents" / "agent" / "runtime" / "package_root.py"
                    for root in (here.parent, *here.parents)
                    if (root / ".agents" / "agent" / "runtime" / "package_root.py").is_file()), None)
    if locator is None:
        raise RuntimeError("Agent peer runtime is unavailable")
    spec = importlib.util.spec_from_file_location("agent_close_package_root", locator)
    package_root = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package_root)
    root = package_root.find_package_root(here)
    path = root / ".agents" / "agent" / "utility" / "peer" / "scripts" / "session_close.py"
    if not path.is_file():
        raise RuntimeError("Agent peer runtime is unavailable")
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("agent_close_session_close", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Agent peer runtime is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def signal(args: argparse.Namespace) -> int:
    write_object(args.path, {"token": args.token} if args.command == "complete" else {"cancelled": True, "reason": args.reason})
    print(json.dumps({"status": args.command, "path": str(args.path)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare")
    command.add_argument("--session-id", required=True)
    command.add_argument("--profile", required=True)
    command.add_argument("--record", type=Path)
    command.add_argument("--target", type=Path, required=True)
    command.add_argument("--transcript", type=Path)
    command = commands.add_parser("arm")
    command.add_argument("--host", required=True)
    command.add_argument("--session-id", required=True)
    command.add_argument("--turn-id")
    command.add_argument("--transcript", type=Path)
    command.add_argument("--target", type=Path, required=True)
    command.add_argument("--profile", required=True)
    command.add_argument("--record", type=Path)
    command.add_argument("--cli-close", type=Path, required=True)
    command.add_argument("--completion", type=Path, required=True)
    command.add_argument("--cancel", type=Path, required=True)
    command.add_argument("--completion-token")
    command.add_argument("--state", type=Path)
    for name in ("complete", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("--path", type=Path, required=True)
        command.add_argument("--token", required=name == "complete")
        command.add_argument("--reason", default="new prompt" if name == "cancel" else None)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            return prepare(args)
        if args.command == "arm":
            return arm(args)
        return signal(args)
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"status": "unavailable" if args.command == "prepare" else "refused", "reason": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
