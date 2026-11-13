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
import plugin_update
from tj_agent.host import Host
from tj_cli import process
from tree_digest import Digests


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tj plugins", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    update = commands.add_parser("update", help="Refresh this host's plugins only")
    update.add_argument("--host", type=host_argument, choices=list(Host))
    arguments = parser.parse_args(argv)
    try:
        host = selected_host(arguments.host, os.environ)
    except ValueError as error:
        parser.error(str(error))
    outcome = plugin_update.update(host, Digests(jobs.state_directory()))
    if outcome.failures:
        for failure in outcome.failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    if outcome.changed and outcome.revision:
        failure = publish(host, outcome.revision)
        if failure:
            print(f"{host}: tj update failed: {failure}", file=sys.stderr)
            return 1
    print("tj: plugin update done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
