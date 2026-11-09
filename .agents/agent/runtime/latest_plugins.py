#!/usr/bin/env python3
"""Refuse work while this session, its plugins or the machine are behind their pins.

Each cause starts only its own fix: plugins behind their pin start the plugin refresh,
and machine drift starts only the install steps that repair it.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jobs
import plugin_copies
import retired_state
import work_items_install
from claude import installed as claude_installed
from codex import installed as codex_installed
from tj_agent.host import Host

MARKETPLACES = ("tj-agents", "agents")
RUNTIME = Path(__file__).resolve().parent
TJ_ITEM = "tj item CLI"
PLUGIN_COPIES = "plugin copies"
DRIFT_NOUNS = {TJ_ITEM: "tools", PLUGIN_COPIES: "plugin copies"}


class Installed(Protocol):
    RELOAD: str

    def installed_build_behind(
        self, marketplace: str, plugin: str, install: Path
    ) -> str | None: ...

    def loaded_build_behind(
        self, root: Path, pin: str, install: Path
    ) -> str | None: ...


INSTALLED: Mapping[Host, Installed] = {
    Host.CLAUDE: claude_installed,
    Host.CODEX: codex_installed,
}


def installed_behind(host: Host) -> list[str]:
    found = []
    for marketplace in MARKETPLACES:
        pins = claude_installed.pins(marketplace)
        installs = claude_installed.installs(marketplace)
        for plugin, pin in sorted(pins.items()):
            if plugin not in installs:
                continue
            revision, path = installs[plugin]
            behind = (
                f"installed {revision[:12]}, marketplace {pin[:12]}"
                if revision != pin
                else INSTALLED[host].installed_build_behind(marketplace, plugin, path)
            )
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


def packaged(relative: str) -> Path:
    agent = claude_installed.installs("tj-agents").get("agent")
    script = agent[1] / ".agents" / "agent" / "runtime" / relative if agent else None
    return script if script is not None and script.is_file() else RUNTIME / relative


def refresh_command() -> list[str]:
    return [sys.executable, "-B", str(packaged("plugins/refresh.py"))]


def install_command(steps: Iterable[str]) -> list[str]:
    selected = [argument for step in steps for argument in ("--step", step)]
    return [sys.executable, "-B", str(packaged("install.py")), *selected]


def tools_behind() -> list[str]:
    pins = work_items_install.Pins.load()
    directory = work_items_install.default_tool_directory(os.environ, os.name == "nt")
    return work_items_install.drift(
        directory, pins, work_items_install.installed_plugins()
    )


def machine_drift(state: Path) -> dict[str, list[str]]:
    drift = {TJ_ITEM: tools_behind(), PLUGIN_COPIES: plugin_copies.drift(state)}
    return {step: found for step, found in drift.items() if found}


def fixing(
    state: Path,
    job: jobs.Job,
    command: Sequence[str],
    behind: str,
    then: str,
    rerun: str,
) -> str:
    running = jobs.running(state, job)
    failure = None if running else jobs.recent_failure(state, job)
    if failure:
        return f"tj-agents {job.label} failed ({failure}); {rerun}."
    if not running and not jobs.start(state, job, command):
        return f"tj-agents {behind} are behind and tj_hook is missing; {rerun}."
    return f"Updating tj-agents {behind}… {then} when this says done."


def repair_machine(state: Path) -> str | None:
    drift = machine_drift(state)
    if not drift:
        return None
    command = install_command(drift)
    behind = "; ".join(
        f"{DRIFT_NOUNS[step]} ({'; '.join(found)})" for step, found in drift.items()
    )
    rerun = f"run {shlex.join(command)}"
    return fixing(
        state, jobs.Job.INSTALL, command, behind, "send the prompt again", rerun
    )


def reason(host: Host, state: Path) -> str | None:
    machine = repair_machine(state)
    if installed_behind(host):
        return fixing(
            state,
            jobs.Job.REFRESH,
            refresh_command(),
            "plugins",
            INSTALLED[host].RELOAD,
            "run agent:refresh-plugins",
        )
    if running_behind(host):
        return f"Plugins updated: {INSTALLED[host].RELOAD}."
    return machine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Host, choices=list(Host), required=True)
    arguments = parser.parse_args(argv)
    try:
        event = json.load(sys.stdin).get("hook_event_name", "")
    except (ValueError, AttributeError):
        event = ""
    state = jobs.state_directory()
    if event in ("", "SessionStart"):
        retired_state.remove(state)
    refusal = reason(arguments.host, state)
    if refusal is None:
        return 0
    if event == "UserPromptSubmit":
        print(json.dumps({"decision": "block", "reason": refusal}))
    else:
        context = {
            "hookEventName": event or "SessionStart",
            "additionalContext": refusal,
        }
        print(json.dumps({"hookSpecificOutput": context}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
