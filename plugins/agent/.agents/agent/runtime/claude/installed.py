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
UPDATE = ("claude", "plugin", "update")


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
    report.steps(
        HOST,
        [
            [
                (
                    f"marketplace {marketplace}",
                    ["claude", "plugin", "marketplace", "update", marketplace],
                )
            ]
            for marketplace in marketplaces
            if isinstance(known, dict) and marketplace in known
        ],
    )


def registered(marketplaces: tuple[str, ...]) -> dict[str, list[str]]:
    registry = read(HOST.configuration_root / "plugins" / "installed_plugins.json")
    plugins = registry.get("plugins", {}) if isinstance(registry, dict) else {}
    found: dict[str, list[str]] = {}
    for plugin_id, rows in sorted(plugins.items()):
        if plugin_id.rpartition("@")[2] not in marketplaces or not isinstance(
            rows, list
        ):
            continue
        for row in rows:
            if (
                isinstance(row, dict)
                and isinstance(row.get("scope"), str)
                and project_plugins.applies(
                    Path.cwd(), row["scope"], row.get("projectPath")
                )
                and row["scope"] not in found.get(plugin_id, [])
            ):
                found.setdefault(plugin_id, []).append(row["scope"])
    return found


def stale(plugins: dict[str, list[str]]) -> dict[str, list[str]]:
    found = {}
    for plugin, scopes in plugins.items():
        name, _, marketplace = plugin.rpartition("@")
        behind = [scope for scope in scopes if not pinned(marketplace, name, scope)]
        if behind:
            found[plugin] = behind
    return found


def refresh(
    report: Any, marketplaces: tuple[str, ...], current: Callable[[str, str], bool]
) -> None:
    plugins = registered(marketplaces)
    behind = stale(plugins)
    if len(behind) < len(plugins):
        print(f"{HOST}: {len(plugins) - len(behind)} plugin(s) already at their pin")
    report.steps(
        HOST,
        [
            [
                (
                    plugin,
                    [*UPDATE, plugin, "--scope", scope, "--json", "-y"],
                )
                for scope in scopes
            ]
            for plugin, scopes in behind.items()
        ],
    )
    if report.failures:
        return
    for plugin, scopes in stale(behind).items():
        name, _, marketplace = plugin.rpartition("@")
        if name in pins(marketplace):
            report.fail(
                HOST, plugin, f"{', '.join(scopes)} did not reach its marketplace pin"
            )


def activate(report: Any) -> None:
    return None
