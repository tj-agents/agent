#!/usr/bin/env python3
"""Refuse work while this session or its installed plugins are behind their marketplace pins (Python 3.9+)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude import installed as claude_installed
from codex import installed as codex_installed
from tj_agent.host import Host

MARKETPLACES = ("tj-agents", "agents")
RUNTIME = Path(__file__).resolve().parent
INSTALLED = {Host.CLAUDE: claude_installed, Host.CODEX: codex_installed}


def installed_behind(host: Host) -> list[str]:
    found = []
    for marketplace in MARKETPLACES:
        pins = claude_installed.pins(marketplace)
        installs = claude_installed.installs(marketplace)
        for plugin, pin in sorted(pins.items()):
            if plugin not in installs:
                continue
            revision, path = installs[plugin]
            behind = (f"installed {revision[:12]}, marketplace {pin[:12]}" if revision != pin
                      else INSTALLED[host].installed_build_behind(marketplace, plugin, path))
            if behind:
                found.append(f"{plugin}@{marketplace}: {behind}")
    return found


def running_root(host: Host) -> Path:
    value = os.environ.get(host.plugin_root_variable)
    return Path(value) if value else RUNTIME.parents[2]


def running_behind(host: Host) -> str | None:
    pin = claude_installed.pins("tj-agents").get("agent")
    install = claude_installed.installs("tj-agents").get("agent")
    if not pin or not install or install[0] != pin:
        return None
    behind = INSTALLED[host].loaded_build_behind(running_root(host), pin, install[1])
    return f"agent@tj-agents: {behind}" if behind else None


def refresh_command(host: Host) -> str:
    agent = claude_installed.installs("tj-agents").get("agent")
    script = agent[1] / ".agents" / "agent" / "runtime" / "refresh.py" if agent else RUNTIME / "refresh.py"
    if not script.is_file():
        script = RUNTIME / "refresh.py"
    python = "python" if os.name == "nt" else "python3"
    return f'{INSTALLED[host].COMMAND_PREFIX}{python} -B "{script.as_posix()}"'


def reason(host: Host) -> str | None:
    behind = installed_behind(host)
    if behind:
        return ("tj-agents plugins are behind the marketplace: " + "; ".join(behind)
                + f". Do no other work. Refresh with `{refresh_command(host)}`, then start a new session.")
    loaded = running_behind(host)
    if loaded:
        return (f"This session runs older plugins than are installed ({loaded}). "
                f"Do no other work; {INSTALLED[host].RELOAD}.")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Host, choices=list(Host), required=True)
    arguments = parser.parse_args(argv)
    try:
        event = json.load(sys.stdin).get("hook_event_name", "")
    except (ValueError, AttributeError):
        event = ""
    refusal = reason(arguments.host)
    if refusal is None:
        return 0
    if event == "UserPromptSubmit":
        print(json.dumps({"decision": "block", "reason": refusal}))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event or "SessionStart",
                                                 "additionalContext": refusal}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
