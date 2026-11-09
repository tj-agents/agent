#!/usr/bin/env python3
"""Refuse work while this session or its installed plugins are behind their marketplace pins (Python 3.9+)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude import installed as claude_installed
from codex import installed as codex_installed
import refresh_lock
import retired_machine_events
from tj_agent.host import Host
from tj_cli import process

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


def refresh_script() -> Path:
    agent = claude_installed.installs("tj-agents").get("agent")
    script = agent[1] / ".agents" / "agent" / "runtime" / "refresh.py" if agent else None
    return script if script and script.is_file() else RUNTIME / "refresh.py"


def start_refresh() -> bool:
    log = refresh_lock.state_directory() / "plugin-refresh.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    detached = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                else {"start_new_session": True})
    try:
        with log.open("ab") as output:
            process.start([sys.executable, "-B", str(refresh_script())], stdin=subprocess.DEVNULL, stdout=output,
                          stderr=subprocess.STDOUT, close_fds=True, **detached)
    except FileNotFoundError:
        return False
    return True


def reason(host: Host) -> str | None:
    if installed_behind(host):
        failure = None if refresh_lock.running() else refresh_lock.recent_failure()
        if failure:
            return f"tj-agents plugin refresh failed ({failure}); run agent:refresh-plugins."
        if not refresh_lock.running() and not start_refresh():
            return "tj-agents plugins are behind and tj_hook is missing; run agent:refresh-plugins."
        return f"Updating tj-agents plugins… {INSTALLED[host].RELOAD} when this says done."
    if running_behind(host):
        return f"Plugins updated: {INSTALLED[host].RELOAD}."
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Host, choices=list(Host), required=True)
    arguments = parser.parse_args(argv)
    try:
        event = json.load(sys.stdin).get("hook_event_name", "")
    except (ValueError, AttributeError):
        event = ""
    if event in ("", "SessionStart"):
        retired_machine_events.remove(retired_machine_events.state_directory())
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
