#!/usr/bin/env python3
"""Prepare or signal an exact CLI close without inferring a frontend identity."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys


TRANSCRIPT_TAIL_BYTES = 256 * 1024

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
    # Legacy registry pid is host bookkeeping and can be a shared daemon. Only a
    # separately recorded frontend identity is eligible for native closure.
    frontend = identity(record.get("frontend"), "frontend")
    terminal = record.get("terminal")
    if not isinstance(terminal, dict) or not isinstance(terminal.get("kind"), str):
        raise ValueError("terminal identity is unavailable")
    wrapper = identity(terminal.get("binding"), "launch wrapper")
    terminal = {key: value for key, value in terminal.items() if key != "binding"}
    target = {"schema_version": 1, "frontend": frontend, "wrapper": wrapper, "terminal": terminal}
    return target


def verified_receipt(record: dict, session_id: str, profile: str) -> None:
    provenance = record.get("provenance")
    if (record.get("session_id") != session_id or record.get("profile") != profile
            or not isinstance(provenance, dict) or provenance.get("source") != "agent-startup-binding"
            or not isinstance(provenance.get("receipt"), str)):
        raise ValueError("record is not the verified startup binding for this session and profile")
    path = Path(provenance["receipt"]).resolve()
    root = (state_root() / "recovery-launches").resolve()
    if root not in path.parents:
        raise ValueError("startup binding receipt is outside private runtime state")
    receipt = read_object(path)
    wrapper = receipt.get("wrapper")
    if not (receipt.get("session") == session_id and receipt.get("profile") == profile
            and receipt.get("state") == "launched" and isinstance(wrapper, dict)
            and wrapper.get("child") == record.get("frontend") and wrapper.get("terminal") == record.get("terminal")):
        raise ValueError("startup binding receipt does not prove this record")


def state_root() -> Path:
    return Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state")


def _transcript_records(path: Path) -> list[dict]:
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - TRANSCRIPT_TAIL_BYTES))
        raw = stream.read().splitlines()
    records = []
    for line in raw:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _is_claude_user(record: dict) -> bool:
    if record.get("type") != "user" or record.get("isMeta") or record.get("isCompactSummary"):
        return False
    content = (record.get("message") or {}).get("content")
    return isinstance(content, str) or (isinstance(content, list) and not any(
        isinstance(item, dict) and item.get("type") == "tool_result" for item in content))


def claude_turn(path: Path) -> str:
    for record in reversed(_transcript_records(path)):
        token = record.get("uuid")
        if _is_claude_user(record) and isinstance(token, str) and token:
            return token
    raise ValueError("Claude transcript has no bounded, non-sidechain user turn")


def claude_transcript(session_id: str, supplied: Path | None) -> Path:
    if supplied is not None:
        return supplied.resolve()
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    matches = list(root.glob("*/" + session_id + ".jsonl")) if root.is_dir() else []
    if len(matches) != 1:
        raise ValueError("exact Claude transcript is unavailable")
    return matches[0].resolve()


def record_path(session_id: str) -> Path:
    if not session_id or any(character in session_id for character in "\\/"):
        raise ValueError("session id must be one registry filename component")
    return state_root() / "cli-sessions" / f"{session_id}.json"


def prepare(args: argparse.Namespace) -> int:
    record = read_object(args.record or record_path(args.session_id))
    verified_receipt(record, args.session_id, args.profile)
    target = target_from_record(record)
    write_object(args.target, target)
    print(json.dumps({"status": "prepared", "target": str(args.target)}))
    return 0


def arm(args: argparse.Namespace) -> int:
    record_path(args.session_id)  # Validate the state filename before creating lifecycle state.
    target = read_object(args.target)
    # Validate before invoking the separately declared CLI capability.
    target_from_record({"frontend": target.get("frontend"), "terminal": {
        **(target.get("terminal") if isinstance(target.get("terminal"), dict) else {}),
        "binding": target.get("wrapper"),
    }})
    token = args.completion_token or secrets.token_urlsafe(24)
    command = [sys.executable, "-B", str(args.cli_close), "arm", "--target", str(args.target),
               "--receipt", str(args.receipt), "--completion", str(args.completion),
               "--completion-token", token, "--cancel", str(args.cancel)]
    try:
        completed = subprocess.run(command, text=True, encoding="utf-8", capture_output=True, check=False, timeout=20)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("cli close arm timed out") from error
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "cli close arm failed")
    state = {
        "host": args.host, "session_id": args.session_id, "turn_id": args.turn_id,
        "completion": str(args.completion), "cancel": str(args.cancel), "completion_token": token,
    }
    if args.host == "claude":
        transcript = claude_transcript(args.session_id, args.transcript)
        state.update({"transcript": str(transcript), "claude_user_uuid": claude_turn(transcript)})
    write_object(args.state or state_root() / "close-lifecycle" / f"{args.session_id}.json", state)
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
    command.add_argument("--profile", required=True)
    command.add_argument("--record", type=Path)
    command.add_argument("--target", type=Path, required=True)
    command = commands.add_parser("arm")
    command.add_argument("--host", choices=("claude", "codex"), required=True)
    command.add_argument("--session-id", required=True)
    command.add_argument("--turn-id")
    command.add_argument("--transcript", type=Path)
    command.add_argument("--target", type=Path, required=True)
    command.add_argument("--cli-close", type=Path, required=True)
    command.add_argument("--receipt", type=Path, required=True)
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
