#!/usr/bin/env python3
"""Read or toggle Codex auto-memory without discarding unrelated JSON settings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def settings(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "toggle"))
    parser.add_argument("--project", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    path = args.project.resolve() / ".codex" / "settings.local.json"
    try:
        value = settings(path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"auto-memory unavailable: {error}", file=sys.stderr)
        return 1
    enabled = value.get("autoMemoryEnabled", True)
    if not isinstance(enabled, bool):
        print("auto-memory unavailable: autoMemoryEnabled must be a boolean", file=sys.stderr)
        return 1
    if args.command == "status":
        print(json.dumps({"auto_memory": "on" if enabled else "off", "settings": str(path)}))
        return 0
    value["autoMemoryEnabled"] = not enabled
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"auto-memory unavailable: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"auto_memory": "on" if not enabled else "off", "settings": str(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
