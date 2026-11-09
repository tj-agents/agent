#!/usr/bin/env python3
"""Bring the shared Claude and Codex plugin installs up to the marketplace, then restart background sessions."""

from __future__ import annotations

import argparse
from functools import partial
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import refresh_lock
import required_plugins
from claude import installed as claude_installed
from codex import installed as codex_installed
from tj_agent.host import Host
from tj_cli import process

MARKETPLACES = ("tj-agents", "agents")
TIMEOUT_SECONDS = 600
INSTALLED = {Host.CLAUDE: claude_installed, Host.CODEX: codex_installed}
RUNTIME = Path(__file__).resolve().parent
MACHINE_STEPS = (
    "tj lifecycle commands",
    "shell profiles",
    "tj item CLI",
    "worktrunk",
)


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def run(
        self, command: list[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return process.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            check=False,
            cwd=cwd,
        )

    def step(
        self,
        host: Host | str,
        label: str,
        command: list[str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            result = self.run(command, cwd=cwd)
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
    steps = [argument for step in MACHINE_STEPS for argument in ("--step", step)]
    report.step(
        "tj",
        "native CLI, launch runtime, lifecycle commands, shell profiles and tools",
        [sys.executable, "-B", str(RUNTIME / "install.py"), *steps],
    )


def converge_plugins(report: Report) -> None:
    for marketplace in MARKETPLACES:
        installs = sorted(claude_installed.installs(marketplace).items())
        for plugin, (_revision, root) in installs:
            if not current(Host.CLAUDE, marketplace, plugin):
                continue
            identity = f"{plugin}@{marketplace}"
            try:
                target = required_plugins.declaration(root, identity)
            except required_plugins.PluginSetupError as error:
                report.fail("tj", f"{identity} converge", str(error))
                continue
            if target is None or not target.converge:
                continue
            report.step(
                "tj",
                f"{identity} converge",
                [sys.executable, "-B", str(target.script), "converge"],
                cwd=root,
            )


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
        converge_plugins(report)
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
