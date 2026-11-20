#!/usr/bin/env python3
"""Refresh this host's tj-agents plugins without a full machine install."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jobs
import plugin_references
import plugin_update
from tj_agent.host import Host
from tj_cli import process
from tree_digest import Digests

GIT_COMMAND_CONFIGURATION = frozenset({"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"})
GIT_COMMAND_ENTRIES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def host_argument(value: str) -> Host:
    try:
        return {host.value: host for host in Host}[value]
    except KeyError as error:
        raise argparse.ArgumentTypeError("Choose claude or codex") from error


def selected_host(explicit: Host | None, environ: Mapping[str, str]) -> Host:
    if explicit is not None:
        return explicit
    hosts = [host for host in Host if environ.get(host.session_variable)]
    if len(hosts) != 1:
        raise ValueError(
            "Pass --host claude or --host codex outside an unambiguous agent session"
        )
    return hosts[0]


def publish(host: Host, revision: str) -> str | None:
    try:
        result = process.run(
            ["tj", "update", "--host", host.value, "--revision", revision],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return str(error)
    return (
        (result.stderr.strip() or f"exit {result.returncode}")
        if result.returncode
        else None
    )


def refresh_runtime(host: Host) -> str | None:
    """Converge the native pair and launch bindings using the updated package's pins."""
    state = jobs.state_directory()
    package = plugin_update.agent_package(host, state)
    if package is None:
        return (
            "the updated Agent package is unavailable; "
            "runtime convergence is unverified"
        )
    command = [sys.executable, "-B", str(package / ".agents/agent/runtime/install.py")]
    for step in (
        "native CLI",
        "agent-store CLI",
        "tj lifecycle commands",
        "shell profiles",
    ):
        command.extend(("--step", step))
    try:
        for mode in ((), ("--check",)):
            result = process.run(
                [*command, *mode],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=300,
                check=False,
            )
            if result.returncode:
                return (
                    result.stderr or result.stdout
                ).strip() or f"installer exit {result.returncode}"
    except (OSError, subprocess.SubprocessError) as error:
        return str(error)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tj plugins", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    update = commands.add_parser(
        "update", help="Refresh plugins and native launch runtime"
    )
    update.add_argument("--host", type=host_argument, choices=list(Host))
    audit = commands.add_parser(
        "audit",
        help="Find a retired identity in projects, worktrees and host settings",
    )
    audit.add_argument("--identity", action="append", required=True)
    audit.add_argument(
        "--root",
        type=Path,
        action="append",
        help="Additional repository or parent directory to scan",
    )
    arguments = parser.parse_args(argv)
    if arguments.command == "audit":
        return plugin_references.audit(
            arguments.identity, arguments.root or [Path.cwd()]
        )
    try:
        host = selected_host(arguments.host, os.environ)
    except ValueError as error:
        parser.error(str(error))
    # Machine maintenance follows the current installed credential configuration,
    # not one Git command's overrides inherited from a long-running session.
    for name in tuple(os.environ):
        if name.upper() in GIT_COMMAND_CONFIGURATION or name.upper().startswith(
            GIT_COMMAND_ENTRIES
        ):
            del os.environ[name]
    outcome = plugin_update.update(
        host,
        Digests(jobs.state_directory()),
        os.environ,
        converge_runtime=lambda: refresh_runtime(host),
    )
    if outcome.failures:
        for failure in outcome.failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    if outcome.revision:
        problem = publish(host, outcome.revision)
        if problem:
            print(f"{host}: tj update failed: {problem}", file=sys.stderr)
            return 1
    print("tj: plugin update done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
