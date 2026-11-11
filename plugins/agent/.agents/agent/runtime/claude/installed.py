"""Claude's installed plugin builds against its marketplace pins, and their refresh (Python 3.9+)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from claude import project_plugins
from tj_agent.host import Host
from tree_digest import Digests

HOST = Host.CLAUDE
RELOAD = "type /reload-plugins"


def read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def marketplace_root(marketplace: str) -> Path:
    return HOST.configuration_root / "plugins" / "marketplaces" / marketplace


def pins(marketplace: str) -> dict[str, str]:
    catalog = read(marketplace_root(marketplace) / ".claude-plugin" / "marketplace.json")
    entries = catalog.get("plugins", []) if isinstance(catalog, dict) else []
    return {
        entry["name"]: entry["source"]["sha"]
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("source"), dict)
        and isinstance(entry["source"].get("sha"), str)
    }


def scoped_installs(marketplace: str) -> dict[tuple[str, str], tuple[str, Path]]:
    registry = read(HOST.configuration_root / "plugins" / "installed_plugins.json")
    plugins = registry.get("plugins", {}) if isinstance(registry, dict) else {}
    found = {}
    for plugin_id, rows in plugins.items():
        name, _, source = plugin_id.rpartition("@")
        if source != marketplace or not isinstance(rows, list):
            continue
        for row in rows:
            if (
                isinstance(row, dict)
                and isinstance(row.get("scope"), str)
                and isinstance(row.get("gitCommitSha"), str)
                and project_plugins.applies(
                    Path.cwd(), row.get("scope"), row.get("projectPath")
                )
            ):
                found[name, row["scope"]] = (
                    row["gitCommitSha"],
                    Path(row.get("installPath", "")),
                )
    return found


def pinned_install(marketplace: str, plugin: str) -> Path | None:
    pin = pins(marketplace).get(plugin)
    for (name, _scope), (revision, root) in scoped_installs(marketplace).items():
        if name == plugin and pin and revision == pin and root.is_dir():
            return root
    return None


def installs(marketplace: str) -> dict[str, tuple[str, Path]]:
    precedence = {"user": 0, "project": 1, "local": 2}
    ordered = sorted(
        scoped_installs(marketplace).items(), key=lambda item: precedence[item[0][1]]
    )
    return {plugin: installation for (plugin, _scope), installation in ordered}


def identities() -> set[str]:
    registry = read(HOST.configuration_root / "plugins" / "installed_plugins.json")
    plugins = registry.get("plugins", {}) if isinstance(registry, dict) else {}
    return {
        plugin_id
        for plugin_id, rows in plugins.items()
        if isinstance(rows, list) and rows
    }


def refresh_identities(marketplaces: tuple[str, ...]) -> set[str]:
    found = set()
    for marketplace in marketplaces:
        found.update(f"{plugin}@{marketplace}" for plugin in installs(marketplace))
    return found


def pinned(marketplace: str, plugin: str, scope: str | None = None) -> bool:
    pin = pins(marketplace).get(plugin)
    installations = [
        installation
        for (name, installed_scope), installation in scoped_installs(
            marketplace
        ).items()
        if name == plugin and (scope is None or installed_scope == scope)
    ]
    return bool(pin and installations) and all(
        revision == pin and root.is_dir() for revision, root in installations
    )


def current(marketplace: str, plugin: str, digests: Digests) -> bool:
    return pinned(marketplace, plugin)


def update_marketplaces(report: Any, marketplaces: tuple[str, ...]) -> None:
    known = read(HOST.configuration_root / "plugins" / "known_marketplaces.json")
    for marketplace in marketplaces:
        if isinstance(known, dict) and marketplace in known:
            report.step(
                HOST,
                f"marketplace {marketplace}",
                ["claude", "plugin", "marketplace", "update", marketplace],
            )


def refresh(
    report: Any, marketplaces: tuple[str, ...], current: Callable[[str, str], bool]
) -> None:
    listing = report.step(HOST, "list", ["claude", "plugin", "list", "--json"])
    if listing is None:
        return
    plugins = sorted(
        {
            (row["id"], row["scope"])
            for row in json.loads(listing.stdout)
            if row["id"].rpartition("@")[2] in marketplaces
            and project_plugins.applies(
                Path.cwd(), row["scope"], row.get("projectPath")
            )
        }
    )
    stale = [
        (plugin, scope)
        for plugin, scope in plugins
        if not pinned(plugin.rpartition("@")[2], plugin.rpartition("@")[0], scope)
    ]
    if len(stale) < len(plugins):
        print(f"{HOST}: {len(plugins) - len(stale)} plugin(s) already at their pin")
    for plugin, scope in stale:
        report.step(
            HOST,
            plugin,
            ["claude", "plugin", "update", plugin, "--scope", scope, "--json", "-y"],
        )


def activate(report: Any) -> None:
    return None
