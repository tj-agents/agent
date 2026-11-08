#!/usr/bin/env python3
"""Bring the shared Claude and Codex plugin installs up to the marketplace, then restart background sessions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from host import Host
import native_runtime
from tj_cli import process

MARKETPLACES = ("tj-agents", "agents")
TIMEOUT_SECONDS = 600


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return process.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=TIMEOUT_SECONDS, check=False)


def ours(plugin: str) -> bool:
    return plugin.rpartition("@")[2] in MARKETPLACES


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def step(self, host: str, label: str, command: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            result = run(command)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.fail(host, label, str(error))
            return None
        if result.returncode:
            detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
            self.fail(host, label, detail[-1] if detail else f"exit {result.returncode}")
            return None
        print(f"{host}: {label} ok")
        return result

    def fail(self, host: str, label: str, detail: str) -> None:
        self.failures.append(f"{host}: {label}: {detail}")
        print(f"{host}: {label} FAILED: {detail}", file=sys.stderr)


def refresh_claude(report: Report) -> None:
    for marketplace in MARKETPLACES:
        report.step("claude", f"marketplace {marketplace}", ["claude", "plugin", "marketplace", "update", marketplace])
    listing = report.step("claude", "list", ["claude", "plugin", "list", "--json"])
    if listing is None:
        return
    plugins = sorted({row["id"] for row in json.loads(listing.stdout) if row["scope"] == "user" and ours(row["id"])})
    for plugin in plugins:
        report.step("claude", plugin, ["claude", "plugin", "update", plugin, "--scope", "user", "--json", "-y"])


def refresh_codex(report: Report) -> None:
    listing = report.step("codex", "list", ["codex", "plugin", "list", "--json"])
    if listing is None:
        return
    installed = [row for row in json.loads(listing.stdout)["installed"] if row["marketplaceName"] in MARKETPLACES]
    for marketplace in sorted({row["marketplaceName"] for row in installed}):
        report.step("codex", f"marketplace {marketplace}", ["codex", "plugin", "marketplace", "upgrade", marketplace])
    for row in installed:
        report.step("codex", row["pluginId"], ["codex", "plugin", "add", row["pluginId"]])


def refresh_native(report: Report) -> None:
    try:
        changed = native_runtime.prepare()
    except RuntimeError as error:
        report.fail("tj", "native CLI", str(error))
        return
    print(f"tj: native CLI {'updated ' + ', '.join(changed) if changed else 'current'}")


def sessions() -> list[dict]:
    try:
        return json.loads(run(["claude", "agents", "--json"]).stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def respawn(report: Report, rows: list[dict]) -> None:
    for row in rows:
        if row.get("kind") == "background" and row.get("pid") and row.get("id"):
            report.step("claude", f"respawn {row.get('name') or row['id']}", ["claude", "respawn", row["id"]])


def notice(rows: list[dict]) -> None:
    live = [row.get("name") or row.get("sessionId", "")[:8] for row in rows
            if row.get("kind") == "interactive" and row.get("pid")]
    if live:
        print(f"{len(live)} open Claude session(s) still run the old plugins until you type /reload-plugins in them: "
              + ", ".join(live))
    print("Open Codex sessions pick up skill changes on their own; restart them with `codex resume <id>` for hooks.")


REFRESH = {Host.CLAUDE: refresh_claude, Host.CODEX: refresh_codex}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Host, choices=list(Host))
    arguments = parser.parse_args(argv)
    hosts = [arguments.host] if arguments.host else list(Host)
    report = Report()
    for host in hosts:
        REFRESH[host](report)
    refresh_native(report)
    rows = sessions()
    if Host.CLAUDE in hosts:
        respawn(report, rows)
    notice(rows)
    if report.failures:
        print(f"{len(report.failures)} step(s) failed; every other step completed:", file=sys.stderr)
        for failure in report.failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
