#!/usr/bin/env python3
"""Refuse work while installed plugins are behind their marketplace pins (Python 3.9+)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude import installed as claude_installed
from codex import installed as codex_installed

MARKETPLACES = ("tj-agents", "agents")
RUNTIME = Path(__file__).resolve().parent


def codex_label(install: Path) -> str | None:
    try:
        version = json.loads((install / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) else None


def stale(host: str) -> list[str]:
    found = []
    for marketplace in MARKETPLACES:
        pins = claude_installed.pins(marketplace)
        installs = claude_installed.installs(marketplace)
        for plugin, pin in sorted(pins.items()):
            if plugin not in installs:
                continue
            revision, path = installs[plugin]
            if revision != pin:
                found.append(f"{plugin}@{marketplace}: installed {revision[:12]}, marketplace {pin[:12]}")
                continue
            expected = codex_label(path)
            actual = codex_installed.label(marketplace, plugin) if host == "codex" else None
            if expected and actual and actual != expected:
                found.append(f"{plugin}@{marketplace}: installed build {actual}, marketplace build {expected}")
    return found


def refresh_command(host: str) -> str:
    agent = claude_installed.installs("tj-agents").get("agent")
    script = agent[1] / ".agents" / "agent" / "runtime" / "refresh.py" if agent else RUNTIME / "refresh.py"
    if not script.is_file():
        script = RUNTIME / "refresh.py"
    python = "python" if os.name == "nt" else "python3"
    prefix = "! " if host == "claude" else ""
    return f'{prefix}{python} -B "{script.as_posix()}"'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("claude", "codex"), required=True)
    arguments = parser.parse_args(argv)
    try:
        event = json.load(sys.stdin).get("hook_event_name", "")
    except (ValueError, AttributeError):
        event = ""
    behind = stale(arguments.host)
    if not behind:
        return 0
    message = ("tj-agents plugins in this session are behind the marketplace: " + "; ".join(behind)
               + f". Do no other work. Refresh with `{refresh_command(arguments.host)}`, then start a new session.")
    if event == "UserPromptSubmit":
        print(json.dumps({"decision": "block", "reason": message}))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event or "SessionStart",
                                                 "additionalContext": message}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
