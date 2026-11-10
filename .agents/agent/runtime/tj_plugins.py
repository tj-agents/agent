#!/usr/bin/env python3
"""Update the current host's plugins against its local marketplace pins."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jobs
import latest_plugins
from claude import installed as claude_installed
from codex import installed as codex_installed
from tj_agent.host import Host
from tj_cli import process
from tree_digest import Digests

MARKETPLACES = ("tj-agents", "agents")
TIMEOUT_SECONDS = 600


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    @staticmethod
    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
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
        self, host: Host, label: str, command: list[str]
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
    def build_current(
        self, marketplace: str, plugin: str, install: Path, digests: Digests
    ) -> bool: ...

    def refresh(
        self,
        report: Report,
        marketplaces: tuple[str, ...],
        current: Callable[[str, str], bool],
    ) -> None: ...

    def restart(self, report: Report, managed: bool) -> None: ...

    def refresh_identities(self, marketplaces: tuple[str, ...]) -> set[str]: ...


INSTALLED: Mapping[Host, Installed] = {
    Host.CLAUDE: claude_installed,
    Host.CODEX: codex_installed,
}


def current(host: Host, digests: Digests, marketplace: str, plugin: str) -> bool:
    pin = claude_installed.pins(marketplace).get(plugin)
    install = claude_installed.installs(marketplace).get(plugin)
    return bool(
        pin
        and install
        and install[0] == pin
        and install[1].is_dir()
        and INSTALLED[host].build_current(marketplace, plugin, install[1], digests)
    )


def selected_host(explicit: Host | None, environ: Mapping[str, str]) -> Host:
    if explicit is not None:
        return explicit
    hosts = [host for host in Host if environ.get(host.session_variable)]
    if len(hosts) != 1:
        raise ValueError(
            "Pass --host claude or --host codex outside an unambiguous agent session"
        )
    return hosts[0]


def already_current(host: Host, digests: Digests) -> bool:
    identities = {
        identity
        for identity in INSTALLED[host].refresh_identities(MARKETPLACES)
        if identity.rpartition("@")[2] in MARKETPLACES
    }
    return bool(identities) and all(
        current(host, digests, identity.rpartition("@")[2], identity.rpartition("@")[0])
        for identity in sorted(identities)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tj plugins", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    update = commands.add_parser(
        "update", help="Update and activate this host's plugins"
    )
    update.add_argument("--host", type=Host, choices=list(Host))
    arguments = parser.parse_args(argv)
    try:
        host = selected_host(arguments.host, os.environ)
    except ValueError as error:
        parser.error(str(error))
    state = jobs.state_directory()
    with jobs.held(state, jobs.Job.UPDATE) as acquired:
        if not acquired:
            print("tj: another plugin update is running")
            return 0
        report = Report()
        digests = Digests(state)
        if already_current(host, digests):
            digests.save()
            print(f"tj: {host} plugins already match their local pins")
            if os.environ.get(
                host.plugin_root_variable
            ) and latest_plugins.running_behind(host):
                INSTALLED[host].restart(
                    report, bool(os.environ.get("TJ_RUN_LAUNCH_ID"))
                )
            jobs.record(state, jobs.Job.UPDATE, report.failures)
            return int(bool(report.failures))
        INSTALLED[host].refresh(report, MARKETPLACES, partial(current, host, digests))
        digests.save()
        if not report.failures:
            INSTALLED[host].restart(report, bool(os.environ.get("TJ_RUN_LAUNCH_ID")))
        jobs.record(state, jobs.Job.UPDATE, report.failures)
    if report.failures:
        print(
            f"{len(report.failures)} step(s) failed; every other step completed:",
            file=sys.stderr,
        )
        for failure in report.failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("tj: plugin update done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
