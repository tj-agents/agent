"""Claude's installed plugin builds against its marketplace pins, and their refresh (Python 3.9+)."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Callable

from claude import plugins, project_plugins, registry
import plugin_payload
import required_plugins
from tj_agent.host import Host
from tree_digest import Digests

HOST = Host.CLAUDE
RELOAD = "type /reload-plugins"
UPDATE = ("claude", "plugin", "update")
INSTALL = ("claude", "plugin", "install")


def read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def marketplace_root(marketplace: str) -> Path:
    return HOST.configuration_root / "plugins" / "marketplaces" / marketplace


def pins(marketplace: str) -> dict[str, str]:
    catalog = read(
        marketplace_root(marketplace) / ".claude-plugin" / "marketplace.json"
    )
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
    applies = project_plugins.matcher(Path.cwd())
    for plugin_id, rows in plugins.items():
        name, _, source = plugin_id.rpartition("@")
        if source != marketplace or not isinstance(rows, list):
            continue
        for row in rows:
            if (
                isinstance(row, dict)
                and isinstance(row.get("scope"), str)
                and isinstance(row.get("gitCommitSha"), str)
                and applies(row["scope"], row.get("projectPath"))
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
        revision == pin and readable(root, plugin) for revision, root in installations
    )


def readable(root: Path, plugin: str) -> bool:
    return required_plugins.readable_payload(root, HOST, plugin)


def torn(marketplace: str, plugin: str, scope: str) -> bool:
    """An enabled user install at its pin whose cached payload is incomplete.

    Reinstalling enables the plugin and writes user settings, so only an enabled user
    install is reinstalled; other scopes keep the plain update.
    """
    if (
        scope != "user"
        or plugins.user_enabled().get(f"{plugin}@{marketplace}") is not True
    ):
        return False
    pin = pins(marketplace).get(plugin)
    installation = scoped_installs(marketplace).get((plugin, scope))
    return (
        pin is not None
        and installation is not None
        and installation[0] == pin
        and not readable(installation[1], plugin)
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


def pinned_sources(marketplace: str) -> dict[str, plugin_payload.Source]:
    catalog = read(marketplace_root(marketplace) / ".claude-plugin" / "marketplace.json")
    entries = catalog.get("plugins", []) if isinstance(catalog, dict) else []
    found = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            source = plugin_payload.Source.parse(entry.get("source"))
            if source is not None:
                found[entry["name"]] = source
    return found


def build_name(source: plugin_payload.Source) -> str:
    """The cache folder `claude plugin update` names a git-subdir build: commit, then path digest."""
    return f"{source.revision[:12]}-{hashlib.sha256(source.path.encode()).hexdigest()[:8]}"


def direct_update(behind: dict[str, list[str]]) -> None:
    """Move installed rows behind a git-subdir pin to it without `claude plugin update`.

    Writes the pinned build where Claude caches it, records it in installed_plugins.json as
    Claude does, then checks the rows reached their pin. Anything it cannot write or verify
    stays behind for the native update, with the reason on stderr.
    """
    sources: dict[str, plugin_payload.Source] = {}
    builds: dict[str, registry.Build] = {}
    for plugin, scopes in behind.items():
        name, _, marketplace = plugin.rpartition("@")
        source = pinned_sources(marketplace).get(name)
        if source is None:
            continue
        installs = scoped_installs(marketplace)
        moving = tuple(
            scope for scope in scopes
            if (name, scope) in installs and installs[name, scope][0] != source.revision
        )
        if moving:
            root = HOST.configuration_root / "plugins" / "cache" / marketplace / name / build_name(source)
            sources[plugin] = source
            builds[plugin] = registry.Build(root, source.revision, moving)
    if not builds:
        return
    missing = {plugin for plugin, build in builds.items() if not readable(build.root, plugin.rpartition("@")[0])}
    problems = place({plugin: sources[plugin] for plugin in missing}, builds) if missing else {}
    ready = {plugin: build for plugin, build in builds.items() if plugin not in problems}
    try:
        for build in ready.values():
            (build.root / ".orphaned_at").unlink(missing_ok=True)
        if ready:
            registry.record_builds(
                HOST.configuration_root / "plugins" / "installed_plugins.json",
                ready,
                partial(project_plugins.applies, Path.cwd()),
                datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            )
    except (OSError, ValueError) as error:
        problems.update(dict.fromkeys(ready, f"cannot record the build: {error}"))
        ready = {}
    for plugin, build in ready.items():
        name, _, marketplace = plugin.rpartition("@")
        unpinned = [scope for scope in build.scopes if not pinned(marketplace, name, scope)]
        if unpinned:
            problems[plugin] = f"{', '.join(unpinned)} did not verify at {build.root}"
        else:
            print(f"{HOST}: {plugin} ok (direct)")
    for plugin, problem in sorted(problems.items()):
        print(f"{HOST}: {plugin} direct update failed: {problem}; using native update", file=sys.stderr)


def place(sources: dict[str, plugin_payload.Source], builds: dict[str, registry.Build]) -> dict[str, str]:
    """Check out each source beside the cache and rename it to its build root; why any failed."""
    with tempfile.TemporaryDirectory(
        prefix=".tj-update-", dir=HOST.configuration_root / "plugins", ignore_cleanup_errors=True
    ) as staging:
        exported = plugin_payload.export(sources, Path(staging))
        problems = dict(exported.problems)
        for plugin, payload in exported.payloads.items():
            name = plugin.rpartition("@")[0]
            root = builds[plugin].root
            if not readable(payload, name):
                problems[plugin] = f"{sources[plugin].path} at {sources[plugin].revision} is not a readable plugin named {name}"
                continue
            try:
                root.parent.mkdir(parents=True, exist_ok=True)
                payload.rename(root)
            except OSError as error:
                if not readable(root, name):
                    problems[plugin] = f"cannot place {root}: {error}"
    return problems


def refresh(
    report: Any, marketplaces: tuple[str, ...], current: Callable[[str, str], bool]
) -> None:
    plugins = registered(marketplaces)
    direct_update(stale(plugins))
    behind = stale(plugins)
    if len(behind) < len(plugins):
        print(f"{HOST}: {len(plugins) - len(behind)} plugin(s) already at their pin")
    groups = []
    for plugin, scopes in behind.items():
        name, _, marketplace = plugin.rpartition("@")
        group = []
        for scope in scopes:
            # `update` accepts a torn payload at its pin as current; only `install` rewrites it.
            verb = INSTALL if torn(marketplace, name, scope) else UPDATE
            group.append((plugin, [*verb, plugin, "--scope", scope, "--json", "-y"]))
        groups.append(group)
    report.steps(HOST, groups)
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
