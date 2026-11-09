#!/usr/bin/env python3
"""Bring the shared Claude and Codex plugin installs up to the marketplace, then restart background sessions."""

from __future__ import annotations

import argparse
from functools import partial
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude import installed as claude_installed
from codex import installed as codex_installed
import refresh_lock
from tj_agent.host import Host
from tj_cli import process

MARKETPLACES = ("tj-agents", "agents")
TIMEOUT_SECONDS = 600
INSTALLED = {Host.CLAUDE: claude_installed, Host.CODEX: codex_installed}
RUNTIME = Path(__file__).resolve().parent


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return process.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=TIMEOUT_SECONDS, check=False)

    def step(self, host: Host | str, label: str, command: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            result = self.run(command)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.fail(host, label, str(error))
            return None
        if result.returncode:
            detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
            self.fail(host, label, detail[-1] if detail else f"exit {result.returncode}")
            return None
        print(f"{host}: {label} ok")
        return result

    def fail(self, host: Host | str, label: str, detail: str) -> None:
        self.failures.append(f"{host}: {label}: {detail}")
        print(f"{host}: {label} FAILED: {detail}", file=sys.stderr)


def current(host: Host, marketplace: str, plugin: str) -> bool:
    pin = claude_installed.pins(marketplace).get(plugin)
    install = claude_installed.installs(marketplace).get(plugin)
    return bool(pin and install and install[0] == pin
                and INSTALLED[host].build_current(marketplace, plugin, install[1]))


def refresh_machine(report: Report) -> None:
    report.step("tj", "native CLI, launch runtime, lifecycle commands and shell profiles",
                [sys.executable, "-B", str(RUNTIME / "install.py"), "--step", "tj lifecycle commands",
                 "--step", "shell profiles"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Host, choices=list(Host))
    arguments = parser.parse_args(argv)
    hosts = [arguments.host] if arguments.host else list(Host)
    with refresh_lock.held() as acquired:
        if not acquired:
            print("tj: another plugin refresh is running")
            return 0
        report = Report()
        for host in hosts:
            INSTALLED[host].refresh(report, MARKETPLACES, partial(current, host))
        refresh_machine(report)
        for host in hosts:
            INSTALLED[host].restart(report)
        refresh_lock.record(report.failures)
    if report.failures:
        print(f"{len(report.failures)} step(s) failed; every other step completed:", file=sys.stderr)
        for failure in report.failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("tj: plugin refresh done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
