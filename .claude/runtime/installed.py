"""Claude's installed plugin builds against its marketplace pins, and their refresh (Python 3.9+)."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

from tj_agent.host import Host

HOST = Host.CLAUDE
RELOAD = "type /reload-plugins"
BUILD = re.compile(r"([0-9a-f]{12})-[0-9a-f]+")


def read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def pins(marketplace: str) -> dict[str, str]:
    catalog = read(HOST.configuration_root / "plugins" / "marketplaces" / marketplace / ".claude-plugin"
                   / "marketplace.json")
    entries = catalog.get("plugins", []) if isinstance(catalog, dict) else []
    return {entry["name"]: entry["source"]["sha"] for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("source"), dict)
            and isinstance(entry["source"].get("sha"), str)}


def installs(marketplace: str) -> dict[str, tuple[str, Path]]:
    registry = read(HOST.configuration_root / "plugins" / "installed_plugins.json")
    plugins = registry.get("plugins", {}) if isinstance(registry, dict) else {}
    found = {}
    for plugin_id, rows in plugins.items():
        name, _, source = plugin_id.rpartition("@")
        if source != marketplace or not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("scope") == "user" and isinstance(row.get("gitCommitSha"), str):
                found[name] = (row["gitCommitSha"], Path(row.get("installPath", "")))
    return found


def identities() -> set[str]:
    registry = read(HOST.configuration_root / "plugins" / "installed_plugins.json")
    plugins = registry.get("plugins", {}) if isinstance(registry, dict) else {}
    return {
        plugin_id
        for plugin_id, rows in plugins.items()
        if isinstance(rows, list) and rows
    }


def installed_build_behind(marketplace: str, plugin: str, install: Path) -> str | None:
    return None


def build_current(marketplace: str, plugin: str, install: Path) -> bool:
    return True


def loaded_build_behind(root: Path, pin: str, install: Path) -> str | None:
    build = BUILD.fullmatch(root.name)
    if build and build.group(1) != pin[:12]:
        return f"this session loaded {build.group(1)}, current {pin[:12]}"
    return None


def refresh(report: Any, marketplaces: tuple[str, ...], current: Callable[[str, str], bool]) -> None:
    for marketplace in marketplaces:
        report.step(HOST, f"marketplace {marketplace}", ["claude", "plugin", "marketplace", "update", marketplace])
    listing = report.step(HOST, "list", ["claude", "plugin", "list", "--json"])
    if listing is None:
        return
    plugins = sorted({row["id"] for row in json.loads(listing.stdout)
                      if row["scope"] == "user" and row["id"].rpartition("@")[2] in marketplaces})
    stale = [plugin for plugin in plugins if not current(plugin.rpartition("@")[2], plugin.rpartition("@")[0])]
    if len(stale) < len(plugins):
        print(f"{HOST}: {len(plugins) - len(stale)} plugin(s) already at their pin")
    for plugin in stale:
        report.step(HOST, plugin, ["claude", "plugin", "update", plugin, "--scope", "user", "--json", "-y"])


def sessions(report: Any) -> list[dict]:
    try:
        return json.loads(report.run(["claude", "agents", "--json"]).stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def restart(report: Any) -> None:
    rows = sessions(report)
    for row in rows:
        if row.get("kind") == "background" and row.get("pid") and row.get("id"):
            report.step(HOST, f"respawn {row.get('name') or row['id']}", ["claude", "respawn", row["id"]])
    live = [row.get("name") or row.get("sessionId", "")[:8] for row in rows
            if row.get("kind") == "interactive" and row.get("pid")]
    if live:
        print(f"{len(live)} open Claude session(s) still run the old plugins until you {RELOAD} in them: "
              + ", ".join(live))
