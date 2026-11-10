"""Codex's installed plugin build labels, and their refresh (Python 3.9+)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Protocol

import jobs
import plugin_activation
import required_plugins
import standards
from codex import catalog, hook_trust, launch, user_plugins
from tj_agent.host import Host
from tree_digest import Digests

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


class Server(Protocol):
    def request(self, method: str, params: dict[str, object]) -> dict[str, object]: ...


def loaded_directories(server: Server) -> set[Path]:
    directories = set()
    seen = set()
    cursor: str | None = None
    while True:
        page = server.request("thread/loaded/list", {"cursor": cursor})
        threads = page.get("data")
        if not isinstance(threads, list) or any(
            not isinstance(thread, str) for thread in threads
        ):
            raise RuntimeError("Codex returned an invalid loaded-thread list")
        for identity in threads:
            thread = server.request("thread/read", {"threadId": identity}).get("thread")
            directory = thread.get("cwd") if isinstance(thread, dict) else None
            if not isinstance(directory, str) or not Path(directory).is_absolute():
                raise RuntimeError(
                    f"Codex thread {identity} has no absolute working directory"
                )
            directories.add(Path(directory))
        next_cursor = page.get("nextCursor")
        if next_cursor is None:
            return directories
        if not isinstance(next_cursor, str) or next_cursor in seen:
            raise RuntimeError("Codex returned an invalid loaded-thread cursor")
        seen.add(next_cursor)
        cursor = next_cursor


def update_project_standards(directory: Path) -> None:
    project = required_plugins.project_root(directory)
    if (project / ".agents/repo-capabilities.local.json").is_file():
        return
    selected = required_plugins.selected(project)
    if not selected:
        return
    builds = catalog.recorded(jobs.state_directory())
    digests = Digests(jobs.state_directory())
    roots = {}
    for identity in selected:
        plugin, _, marketplace = identity.rpartition("@")
        build = builds.get(identity)
        root = catalog.root(marketplace, plugin, build.version) if build else None
        if root is None or not catalog.current(marketplace, plugin, digests):
            raise standards.StandardsError(
                f"Cannot verify updated standards for {identity} in {project}"
            )
        roots[identity] = root
    digests.save()
    standards.update(project, roots)


def codex_command(*arguments: str) -> list[str]:
    return ["codex", *arguments, *MARKETPLACE_OVERRIDES]


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


def refresh_identities(marketplaces: tuple[str, ...]) -> set[str]:
    return {
        identity
        for identity in identities()
        if identity.rpartition("@")[2] in marketplaces
    }


def behind(marketplaces: tuple[str, ...]) -> list[str]:
    digests = Digests(jobs.state_directory())
    found = []
    for identity in sorted(refresh_identities(marketplaces)):
        plugin, _, marketplace = identity.rpartition("@")
        if not catalog.current(marketplace, plugin, digests):
            found.append(
                f"{identity}: installed build does not match its verified Codex catalog version"
            )
    digests.save()
    return found


def current(marketplace: str, plugin: str, digests: Digests) -> bool:
    return catalog.current(marketplace, plugin, digests)


def running_behind(root: Path) -> str | None:
    build = catalog.recorded(jobs.state_directory()).get("agent@tj-agents")
    loaded = catalog.label(root)
    if build is not None and loaded != build.version:
        return f"agent@tj-agents: this session loaded build {loaded or 'that was since replaced'}, current {build.version}"
    return None


def refresh(
    report: Any, marketplaces: tuple[str, ...], current: Callable[[str, str], bool]
) -> None:
    listing = report.step(HOST, "list", codex_command("plugin", "list", "--json"))
    if listing is None:
        return
    listed = {
        row["pluginId"]
        for row in json.loads(listing.stdout)["installed"]
        if row["marketplaceName"] in marketplaces
    }
    candidates = listed | cached(marketplaces)
    if required_plugins.MARKETPLACE_NAME in marketplaces and not any(
        identity.endswith("@" + required_plugins.MARKETPLACE_NAME)
        for identity in candidates
    ):
        report.fail(
            HOST, "list", f"no {required_plugins.MARKETPLACE_NAME} plugins installed"
        )
        return
    for marketplace in sorted({identity.rpartition("@")[2] for identity in candidates}):
        report.step(
            HOST,
            f"marketplace {marketplace}",
            codex_command("plugin", "marketplace", "upgrade", marketplace),
        )
    versions = report.step(HOST, "versions", codex_command("plugin", "list", "--json"))
    if versions is None:
        return
    entries = json.loads(versions.stdout)["installed"]
    catalog.remember(entries)
    unlisted = candidates - listed
    published = published_plugins(report) if unlisted else set()
    installed = sorted(listed | (unlisted & published))
    for identity in sorted(unlisted - published):
        print(
            f"{HOST}: {identity} is no longer published; left for plugin setup to remove"
        )
    stale = [
        identity
        for identity in installed
        if not current(identity.rpartition("@")[2], identity.rpartition("@")[0])
    ]
    if len(stale) < len(installed):
        print(f"{HOST}: {len(installed) - len(stale)} plugin(s) already at their build")
    user_enabled = user_plugins.enabled(read_user_config())
    open_sessions = bool(stale) and bool(launch.liveness())
    if open_sessions:
        install_through_daemon(report, stale)
    else:
        for identity in stale:
            report.step(HOST, identity, codex_command("plugin", "add", identity))
    keep_project_scope(
        report, [identity for identity in stale if identity not in user_enabled]
    )
    trust_hooks(report)
    if stale and not report.failures:
        refreshed = report.step(
            HOST, "installed versions", codex_command("plugin", "list", "--json")
        )
        if refreshed is not None:
            verified = [
                entry
                for entry in json.loads(refreshed.stdout)["installed"]
                if entry.get("pluginId") in stale
            ]
            catalog.remember(
                verified,
                verified_install=True,
            )
            digests = Digests(jobs.state_directory())
            listed_versions = {entry["pluginId"] for entry in verified}
            for identity in stale:
                plugin, _, marketplace = identity.rpartition("@")
                if identity not in listed_versions or not catalog.current(
                    marketplace, plugin, digests
                ):
                    report.fail(HOST, identity, "native installation did not converge")
            digests.save()
    if open_sessions and report.failures:
        reload_open_sessions(report)


def cached(marketplaces: tuple[str, ...]) -> set[str]:
    cache = HOST.configuration_root / "plugins" / "cache"
    found = set()
    for marketplace in marketplaces:
        directory = cache / marketplace
        for plugin in directory.iterdir() if directory.is_dir() else ():
            builds = (
                [entry for entry in plugin.iterdir() if entry.is_dir()]
                if plugin.is_dir()
                else []
            )
            if len(builds) == 1 and manifest_name(builds[0]) == plugin.name:
                found.add(f"{plugin.name}@{marketplace}")
    return found


def published_plugins(report: Any) -> set[str]:
    roots = marketplace_roots(report) or {}
    found = set()
    for marketplace, root in roots.items():
        try:
            manifest = json.loads(
                (root / ".agents" / "plugins" / "marketplace.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError):
            continue
        found.update(
            f"{entry['name']}@{marketplace}"
            for entry in manifest.get("plugins", [])
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        )
    return found


def manifest_name(root: Path) -> str | None:
    try:
        name = json.loads(
            (root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        ).get("name")
    except (OSError, ValueError, AttributeError):
        return None
    return name if isinstance(name, str) else None


def read_user_config() -> str:
    path = user_plugins.path()
    return path.read_text(encoding="utf-8-sig") if path.is_file() else ""


def keep_project_scope(report: Any, identities: list[str]) -> None:
    """Undo the user-level enablement `codex plugin add` gives a plugin only projects select."""
    text = read_user_config()
    try:
        scoped = user_plugins.without(text, identities)
    except ValueError as error:
        report.fail(HOST, "plugin scope", str(error))
        return
    if scoped != text:
        user_plugins.path().write_text(scoped, encoding="utf-8")


def trust_hooks(report: Any) -> bool:
    """Trust installed tj-agents hooks so no Codex launch asks to trust them."""
    state = jobs.state_directory()
    if not hook_trust.pending(state):
        return False
    try:
        trusted = hook_trust.trust_installed(
            launch.executable(), state, MARKETPLACE_OVERRIDES
        )
    except (OSError, RuntimeError, ValueError) as error:
        report.fail(HOST, "hook trust", str(error))
        return False
    print(f"{HOST}: hook trust ok ({len(trusted)} hook(s) newly trusted)")
    return True


def marketplace_roots(report: Any) -> dict[str, Path] | None:
    listing = report.step(
        HOST,
        "marketplace roots",
        codex_command("plugin", "marketplace", "list", "--json"),
    )
    if listing is None:
        return None
    try:
        return {
            entry["name"]: Path(entry["root"])
            for entry in json.loads(listing.stdout)["marketplaces"]
        }
    except (ValueError, KeyError, TypeError) as error:
        report.fail(HOST, "marketplace roots", f"unreadable listing: {error}")
        return None


def install_through_daemon(report: Any, identities: list[str]) -> None:
    """Install through the shared daemon, which reloads every loaded thread's plugins, hooks and skills."""
    roots = marketplace_roots(report)
    if roots is None:
        return
    try:
        with launch.daemon(INSTALL_TIMEOUT_SECONDS) as server:
            for directory in loaded_directories(server):
                plugin_activation.begin(
                    jobs.state_directory(),
                    HOST,
                    required_plugins.project_root(directory),
                )
            for identity in identities:
                plugin, _, marketplace = identity.rpartition("@")
                manifest = (
                    roots[marketplace] / ".agents" / "plugins" / "marketplace.json"
                )
                try:
                    server.request(
                        "plugin/install",
                        {"marketplacePath": str(manifest), "pluginName": plugin},
                    )
                except RuntimeError as error:
                    report.fail(HOST, identity, str(error))
                    continue
                print(f"{HOST}: {identity} ok")
    except (OSError, RuntimeError, KeyError) as error:
        report.fail(HOST, "open sessions", str(error))


def reload_open_sessions(report: Any) -> None:
    try:
        with launch.daemon(INSTALL_TIMEOUT_SECONDS) as server:
            projects = {
                required_plugins.project_root(directory)
                for directory in loaded_directories(server)
            }
            completed = []
            state = jobs.state_directory()
            for project in sorted(projects):
                plugin_activation.begin(state, HOST, project)
                try:
                    update_project_standards(project)
                except (
                    OSError,
                    ValueError,
                    required_plugins.RequiredPluginsError,
                    standards.StandardsError,
                ) as error:
                    report.fail(HOST, "repository standards", str(error))
                else:
                    completed.append(project)
            server.request("config/batchWrite", {"edits": [], "reloadUserConfig": True})
            for project in completed:
                plugin_activation.complete(state, HOST, project)
    except (OSError, RuntimeError) as error:
        report.fail(HOST, "open sessions", str(error))


def repair_hooks(report: Any) -> None:
    state = jobs.state_directory()
    if hook_trust.pending(state):
        plugin_activation.begin(state, HOST, required_plugins.project_root(Path.cwd()))
    trust_hooks(report)


def restart(report: Any, managed: bool) -> None:
    if launch.liveness():
        reload_open_sessions(report)
    print(
        "Open Codex sessions in the shared app-server reload updated plugins in place."
    )
