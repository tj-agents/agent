#!/usr/bin/env python3
"""Bring the shared Claude and Codex plugin installs up to their marketplace pins.

Then hand any machine drift the new pins cause to its own install step, and restart
background sessions.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jobs
import latest_plugins
from claude import installed as claude_installed
from codex import installed as codex_installed
from tj_agent.host import Host
from tj_cli import process

MARKETPLACES = ("tj-agents", "agents")
TIMEOUT_SECONDS = 600


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return process.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            check=False,
        )

    def step(
        self, host: Host, label: str, command: Sequence[str]
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            result = self.run(command)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.fail(host, label, str(error))
            return None
        if result.returncode:
            detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
            self.fail(
                host, label, detail[-1] if detail else f"exit {result.returncode}"
            )
            return None
        print(f"{host}: {label} ok")
        return result

    def fail(self, host: Host, label: str, detail: str) -> None:
        self.failures.append(f"{host}: {label}: {detail}")
        print(f"{host}: {label} FAILED: {detail}", file=sys.stderr)


class Installed(Protocol):
    def build_current(self, marketplace: str, plugin: str, install: Path) -> bool: ...

    def refresh(
        self,
        report: Report,
        marketplaces: Collection[str],
        current: Callable[[str, str], bool],
    ) -> None: ...

    def restart(self, report: Report) -> None: ...


INSTALLED: Mapping[Host, Installed] = {
    Host.CLAUDE: claude_installed,
    Host.CODEX: codex_installed,
}


def current(host: Host, marketplace: str, plugin: str) -> bool:
    pin = claude_installed.pins(marketplace).get(plugin)
    install = claude_installed.installs(marketplace).get(plugin)
    return bool(
        pin
        and install
        and install[0] == pin
        and INSTALLED[host].build_current(marketplace, plugin, install[1])
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Host, choices=list(Host))
    arguments = parser.parse_args(argv)
    hosts = [arguments.host] if arguments.host else list(Host)
    state = jobs.state_directory()
    with jobs.held(state, jobs.Job.REFRESH) as acquired:
        if not acquired:
            print("tj: another plugin refresh is running")
            return 0
        report = Report()
        for host in hosts:
            INSTALLED[host].refresh(report, MARKETPLACES, partial(current, host))
        machine = latest_plugins.repair_machine(state, full=True)
        if machine:
            print(f"tj: {machine}")
        for host in hosts:
            INSTALLED[host].restart(report)
        jobs.record(state, jobs.Job.REFRESH, report.failures)
    if report.failures:
        print(
            f"{len(report.failures)} step(s) failed; every other step completed:",
            file=sys.stderr,
        )
        for failure in report.failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("tj: plugin refresh done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
