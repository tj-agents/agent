#!/usr/bin/env python3
"""Prepare or signal an exact CLI close without inferring a frontend identity."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys


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
    if not isinstance(started, (int, float)) or isinstance(started, bool) or started <= 0:
        raise ValueError(f"{label} needs an exact process creation time")
    return {"pid": value["pid"], "started_at": started}


def target_from_record(record: dict) -> dict:
    # Legacy registry pid is host bookkeeping and can be a shared daemon. Only a
    # separately recorded frontend identity is eligible for native closure.
    frontend = identity(record.get("frontend"), "frontend")
    terminal = record.get("terminal")
    if not isinstance(terminal, dict) or not isinstance(terminal.get("kind"), str):
        raise ValueError("terminal identity is unavailable")
    binding = identity(terminal.get("binding"), "terminal binding")
    target = {"schema_version": 1, "frontend": frontend, "terminal": {**terminal, "binding": binding}}
    return target


def state_root() -> Path:
    return Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state")


def record_path(session_id: str) -> Path:
    if not session_id or any(character in session_id for character in "\\/"):
        raise ValueError("session id must be one registry filename component")
    return state_root() / "cli-sessions" / f"{session_id}.json"


def prepare(args: argparse.Namespace) -> int:
    record = read_object(args.record or record_path(args.session_id))
    target = target_from_record(record)
    write_object(args.target, target)
    print(json.dumps({"status": "prepared", "target": str(args.target)}))
    return 0


def arm(args: argparse.Namespace) -> int:
    target = read_object(args.target)
    # Validate before invoking the separately declared CLI capability.
    target_from_record({"frontend": target.get("frontend"), "terminal": target.get("terminal")})
    token = args.completion_token or secrets.token_urlsafe(24)
    command = [sys.executable, "-B", str(args.cli_close), "arm", "--target", str(args.target),
               "--receipt", str(args.receipt), "--completion", str(args.completion),
               "--completion-token", token, "--cancel", str(args.cancel)]
    completed = subprocess.run(command, text=True, encoding="utf-8", capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "cli close arm failed")
    print(json.dumps({"status": "armed", "completion_token": token, "receipt": str(args.receipt)}))
    return 0


def signal(args: argparse.Namespace) -> int:
    write_object(args.path, {"token": args.token} if args.command == "complete" else {"cancelled": True, "reason": args.reason})
    print(json.dumps({"status": args.command, "path": str(args.path)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare")
    command.add_argument("--session-id", required=True)
    command.add_argument("--record", type=Path)
    command.add_argument("--target", type=Path, required=True)
    command = commands.add_parser("arm")
    command.add_argument("--target", type=Path, required=True)
    command.add_argument("--cli-close", type=Path, required=True)
    command.add_argument("--receipt", type=Path, required=True)
    command.add_argument("--completion", type=Path, required=True)
    command.add_argument("--cancel", type=Path, required=True)
    command.add_argument("--completion-token")
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
