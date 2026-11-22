"""Close a caller-specified frontend and native terminal tab (Python 3.9+)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import tj_cli.terminal
from tj_cli.process import identity


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise identity.Refusal("cannot read JSON: " + str(path)) from error
    if not isinstance(value, dict):
        raise identity.Refusal("JSON object required: " + str(path))
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_target(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise identity.Refusal("target must be an object")
    if value.get("schema_version") != 1:
        raise identity.Refusal("target schema_version must be 1")
    identity.valid_identity(value.get("frontend"))
    identity.valid_identity(value.get("wrapper"))
    terminal_value = value.get("terminal")
    if not isinstance(terminal_value, dict) or not isinstance(
        terminal_value.get("kind"), str
    ):
        raise identity.Refusal("target needs terminal object")
    if terminal_value["kind"] == "windows-terminal":
        binding = terminal_value.get("tab_binding")
        if not isinstance(binding, dict):
            raise identity.Refusal("Windows Terminal target needs tab binding evidence")
        if (
            not isinstance(binding.get("runtime_id"), str)
            or not binding["runtime_id"].strip()
            or not isinstance(binding.get("observed_title"), str)
            or not binding["observed_title"].strip()
            or binding.get("evidence") != "windows-uia-unique-title-at-launch"
        ):
            raise identity.Refusal(
                "Windows Terminal tab binding evidence is incomplete"
            )
    return value


def target(path: Path) -> dict[str, Any]:
    return validate_target(_read_json(path))


def receipt(
    path: Path, target_value: dict[str, Any], status: str, **extra: object
) -> None:
    _write_json(
        path,
        {
            "schema_version": 1,
            "operation": "cli-close",
            "status": status,
            "at": time.time(),
            "target": target_value,
            **extra,
        },
    )


def close(target_value: dict[str, Any], receipt_path: Path) -> None:
    try:
        tj_cli.terminal.close(target_value)
        deadline = time.monotonic() + 5
        while (
            identity.matches(target_value["frontend"]) and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        identity.require_exited(target_value["frontend"])
    except identity.Refusal as error:
        receipt(receipt_path, target_value, "refused", reason=str(error))
        raise
    receipt(receipt_path, target_value, "closed")


def _completion_matches(path: Path, token: str, armed_at: float) -> bool:
    try:
        if path.stat().st_mtime < armed_at:
            return False
        value = _read_json(path)
    except OSError:
        return False
    return value.get("token") == token


def _frozen_target(value: str) -> dict[str, Any]:
    try:
        return validate_target(json.loads(value))
    except ValueError as error:
        raise identity.Refusal("armed target JSON is invalid") from error


def watch(args: argparse.Namespace) -> int:
    target_value = _frozen_target(args.target_json)
    armed_at = args.armed_at
    if armed_at <= 0:
        raise identity.Refusal("armed timestamp is invalid")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if args.cancel.exists():
            receipt(
                args.receipt,
                target_value,
                "cancelled",
                reason="caller supplied cancellation",
            )
            return 0
        if _completion_matches(args.completion, args.completion_token, armed_at):
            if args.delay:
                time.sleep(args.delay)
            if args.cancel.exists():
                receipt(
                    args.receipt,
                    target_value,
                    "cancelled",
                    reason="caller supplied cancellation",
                )
                return 0
            close(target_value, args.receipt)
            return 0
        time.sleep(0.1)
    receipt(
        args.receipt,
        target_value,
        "cancelled",
        reason="completion was not supplied before timeout",
    )
    return 0


def arm(args: argparse.Namespace) -> int:
    target_value = target(args.target)
    armed_at = time.time()
    frozen_target = json.dumps(target_value, sort_keys=True, separators=(",", ":"))
    receipt(
        args.receipt,
        target_value,
        "armed",
        armed_at=armed_at,
        completion=str(args.completion),
        cancel=str(args.cancel),
    )
    command = [
        sys.executable,
        "-I",
        "-B",
        str(Path(__file__).with_name("_close_entry.py")),
        "watch",
        "--target-json",
        frozen_target,
        "--armed-at",
        str(armed_at),
        "--receipt",
        str(args.receipt),
        "--completion",
        str(args.completion),
        "--completion-token",
        args.completion_token,
        "--cancel",
        str(args.cancel),
        "--delay",
        str(args.delay),
        "--timeout",
        str(args.timeout),
    ]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=sys.platform != "win32",
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("close", "arm"):
        command = commands.add_parser(name)
        command.add_argument("--target", type=Path, required=True)
        command.add_argument("--receipt", type=Path, required=True)
        if name in {"arm", "watch"}:
            command.add_argument("--completion", type=Path, required=True)
            command.add_argument("--completion-token", required=True)
            command.add_argument("--cancel", type=Path, required=True)
            command.add_argument("--delay", type=float, default=3.0)
            command.add_argument("--timeout", type=float, default=900.0)
    watch_command = commands.add_parser("watch")
    watch_command.add_argument("--target-json", required=True)
    watch_command.add_argument("--armed-at", type=float, required=True)
    watch_command.add_argument("--receipt", type=Path, required=True)
    watch_command.add_argument("--completion", type=Path, required=True)
    watch_command.add_argument("--completion-token", required=True)
    watch_command.add_argument("--cancel", type=Path, required=True)
    watch_command.add_argument("--delay", type=float, default=3.0)
    watch_command.add_argument("--timeout", type=float, default=900.0)
    return result


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 9):
        print("cli:close requires Python 3.9 or newer", file=sys.stderr)
        return 2
    args = parser().parse_args(argv)
    try:
        if args.command == "close":
            close(target(args.target), args.receipt)
            return 0
        return arm(args) if args.command == "arm" else watch(args)
    except (OSError, ValueError, identity.Refusal) as error:
        print("cli:close: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
