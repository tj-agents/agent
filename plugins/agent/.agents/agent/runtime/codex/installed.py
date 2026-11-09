"""Codex's installed plugin build labels, and their refresh (Python 3.9+)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import required_plugins
from tj_agent.host import Host

HOST = Host.CODEX
RELOAD = "restart this session"
INSTALL_TIMEOUT_SECONDS = 600
MARKETPLACE_OVERRIDES = (
    "-c",
    f'marketplaces.{required_plugins.MARKETPLACE_NAME}.source_type="git"',
    "-c",
    f"marketplaces.{required_plugins.MARKETPLACE_NAME}.source="
    f'"https://github.com/{required_plugins.MARKETPLACE_REPOSITORY}.git"',
    "-c",
    f"marketplaces.{required_plugins.MARKETPLACE_NAME}.sparse_paths="
    f"{json.dumps(list(required_plugins.MARKETPLACE_SPARSE_PATHS))}",
)


def codex_command(*arguments: str) -> list[str]:
    return ["codex", *arguments, *MARKETPLACE_OVERRIDES]


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


def identities() -> set[str]:
    cache = HOST.configuration_root / "plugins" / "cache"
    try:
        return {
            f"{plugin.name}@{marketplace.name}"
            for marketplace in cache.iterdir()
            if marketplace.is_dir()
            for plugin in marketplace.iterdir()
            if plugin.is_dir() and any(build.is_dir() for build in plugin.iterdir())
        }
    except OSError:
        return set()


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
    from codex import launch

    listing = report.step(HOST, "list", codex_command("plugin", "list", "--json"))
    if listing is None:
        return
    installed = [row for row in json.loads(listing.stdout)["installed"] if row["marketplaceName"] in marketplaces]
    if required_plugins.MARKETPLACE_NAME in marketplaces and not any(
        row["marketplaceName"] == required_plugins.MARKETPLACE_NAME for row in installed
    ):
        report.fail(
            HOST, "list", f"no {required_plugins.MARKETPLACE_NAME} plugins installed"
        )
        return
    for marketplace in sorted({row["marketplaceName"] for row in installed}):
        report.step(
            HOST,
            f"marketplace {marketplace}",
            codex_command("plugin", "marketplace", "upgrade", marketplace),
        )
    stale = [row for row in installed if not current(row["marketplaceName"], row["pluginId"].rpartition("@")[0])]
    if len(stale) < len(installed):
        print(f"{HOST}: {len(installed) - len(stale)} plugin(s) already at their build")
    if stale and launch.liveness():
        reload_open_sessions(report, [row["pluginId"] for row in stale])
        return
    for row in stale:
        report.step(
            HOST, row["pluginId"], codex_command("plugin", "add", row["pluginId"])
        )


def marketplace_roots(report: Any) -> dict[str, Path] | None:
    listing = report.step(HOST, "marketplace roots", codex_command("plugin", "marketplace", "list", "--json"))
    if listing is None:
        return None
    return {entry["name"]: Path(entry["root"]) for entry in json.loads(listing.stdout)["marketplaces"]}


def reload_open_sessions(report: Any, identities: list[str]) -> None:
    """Install through the shared daemon, which reloads every loaded thread's plugins, hooks and skills."""
    from codex import hook_trust, launch

    roots = marketplace_roots(report)
    if roots is None:
        return
    required = {}
    try:
        with launch.daemon(INSTALL_TIMEOUT_SECONDS) as server:
            for identity in identities:
                plugin, _, marketplace = identity.rpartition("@")
                manifest = roots[marketplace] / ".agents" / "plugins" / "marketplace.json"
                try:
                    server.request("plugin/install", {"marketplacePath": str(manifest), "pluginName": plugin})
                except RuntimeError as error:
                    report.fail(HOST, identity, str(error))
                    continue
                print(f"{HOST}: {identity} ok")
                build = installed_label(marketplace, plugin)
                root = HOST.configuration_root / "plugins" / "cache" / marketplace / plugin / (build or "")
                definitions = hook_trust.required_definitions(root, identity) if build else None
                if definitions:
                    required[identity] = definitions
            hook_trust.apply_trust(launch.executable(), Path.home(), required=required)
            server.request("config/batchWrite", {"edits": [], "reloadUserConfig": True})
    except (OSError, RuntimeError, KeyError) as error:
        report.fail(HOST, "open sessions", str(error))


def restart(report: Any) -> None:
    print("Open Codex sessions in the shared app-server reload updated plugins in place.")
