"""Codex's installed plugin build labels, and their refresh (Python 3.9+)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from tj_agent.host import Host

HOST = Host.CODEX
RELOAD = "restart this session"


def label(root: Path) -> str | None:
    try:
        version = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) else None


def installed_label(marketplace: str, plugin: str) -> str | None:
    directory = HOST.configuration_root / "plugins" / "cache" / marketplace / plugin
    try:
        builds = [entry.name for entry in directory.iterdir() if entry.is_dir()]
    except OSError:
        return None
    return builds[0] if len(builds) == 1 else None


def installed_build_behind(marketplace: str, plugin: str, install: Path) -> str | None:
    expected, actual = label(install), installed_label(marketplace, plugin)
    if expected and actual and actual != expected:
        return f"installed build {actual}, marketplace build {expected}"
    return None


def build_current(marketplace: str, plugin: str, install: Path) -> bool:
    expected = label(install)
    return expected is not None and installed_label(marketplace, plugin) == expected


def loaded_build_behind(root: Path, pin: str, install: Path) -> str | None:
    expected, loaded = label(install), label(root)
    if expected and loaded != expected:
        return f"this session loaded build {loaded or 'that was since replaced'}, current {expected}"
    return None


def refresh(report: Any, marketplaces: tuple[str, ...], current: Callable[[str, str], bool]) -> None:
    listing = report.step(HOST, "list", ["codex", "plugin", "list", "--json"])
    if listing is None:
        return
    installed = [row for row in json.loads(listing.stdout)["installed"] if row["marketplaceName"] in marketplaces]
    for marketplace in sorted({row["marketplaceName"] for row in installed}):
        report.step(HOST, f"marketplace {marketplace}", ["codex", "plugin", "marketplace", "upgrade", marketplace])
    stale = [row for row in installed if not current(row["marketplaceName"], row["pluginId"].rpartition("@")[0])]
    if len(stale) < len(installed):
        print(f"{HOST}: {len(installed) - len(stale)} plugin(s) already at their build")
    for row in stale:
        report.step(HOST, row["pluginId"], ["codex", "plugin", "add", row["pluginId"]])


def restart(report: Any) -> None:
    print("Open Codex sessions pick up skill changes on their own; restart them with `codex resume <id>` for hooks.")
