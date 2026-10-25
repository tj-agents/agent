#!/usr/bin/env python3
"""Prepare or signal an exact CLI close without inferring a frontend identity."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import secrets
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
    if record.get("client_identity") != "managed-input":
        raise ValueError("record is not an attributable managed frontend")
    return {"schema_version": 1, "frontend": identity(
        {"pid": record.get("pid"), "started_at": record.get("pid_started_at")}, "frontend",
    )}


def state_root() -> Path:
    return Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state")


def cli_capability_root(path: Path) -> Path:
    resolved = path.resolve()
    for root in (resolved.parent, *resolved.parents):
        markers = (root / ".git", root / ".codex-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json")
        if not any(marker.exists() for marker in markers):
            continue
        identity = root / ".agents" / "cli" / "utility" / "close" / "scripts" / "process_identity.py"
        if identity.is_file():
            return root
        break
    raise ValueError("declared cli:close capability does not ship process_identity.py")


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
    if record.get("session_id") != args.session_id or record.get("profile") != args.profile:
        raise ValueError("record does not match the requested session and profile")
    target = target_from_record(record)
    write_object(args.target, target)
    print(json.dumps({"status": "prepared", "target": str(args.target)}))
    return 0


def arm(args: argparse.Namespace) -> int:
    record_path(args.session_id)  # Validate the state filename before creating lifecycle state.
    target = read_object(args.target)
    if target.get("schema_version") != 1:
        raise ValueError("target schema is unavailable")
    frontend = identity(target.get("frontend"), "frontend")
    record = read_object(args.record or record_path(args.session_id))
    if record.get("session_id") != args.session_id or record.get("profile") != args.profile:
        raise ValueError("record does not match the requested session and profile")
    entry = dict(record)
    if target_from_record(entry)["frontend"] != frontend:
        raise ValueError("target does not match the attributable frontend")
    shared = _session_close()
    entry["expected_activity"] = shared.awaiting_input_activity(entry)
    token = args.completion_token or secrets.token_urlsafe(24)
    state = {
        "host": args.host, "session_id": args.session_id, "turn_id": args.turn_id,
        "completion": str(args.completion), "cancel": str(args.cancel), "completion_token": token,
    }
    if args.host == "claude":
        transcript = claude_transcript(args.session_id, args.transcript)
        state.update({"transcript": str(transcript), "claude_user_uuid": claude_turn(transcript)})
    state_path = args.state or state_root() / "close-lifecycle" / f"{args.session_id}.json"
    if state_path.exists():
        raise RuntimeError("a close request is already armed for this session")
    write_object(state_path, state)
    request = state_root() / "client-close" / (token + ".json")
    result = state_root() / "client-close" / (token + ".result.json")
    cli_root = cli_capability_root(args.cli_close)
    write_object(request, {"entry": entry, "completion_token": token, "cli_root": str(cli_root)})
    try:
        watcher = shared.start_agent_watcher(
            Path(shared.__file__).resolve().with_name("client_close_watcher.py"),
            ["--request", str(request), "--completion", str(args.completion), "--cancel", str(args.cancel),
             "--result", str(result)],
        )
    except Exception:
        state_path.unlink(missing_ok=True)
        request.unlink(missing_ok=True)
        raise
    print(json.dumps({"status": "armed", "completion_token": token, "watcher_pid": watcher,
                      "result": str(result)}))
    return 0


def _session_close():
    import importlib.util

    path = None
    here = Path(__file__).resolve()
    for root in (here.parent, *here.parents):
        markers = (root / ".git", root / ".codex-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json")
        if any(marker.exists() for marker in markers):
            candidate = root / ".agents" / "agent" / "utility" / "peer" / "scripts" / "session_close.py"
            if candidate.is_file():
                path = candidate
            break
    if path is None:
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
    command = commands.add_parser("arm")
    command.add_argument("--host", choices=("claude", "codex"), required=True)
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
