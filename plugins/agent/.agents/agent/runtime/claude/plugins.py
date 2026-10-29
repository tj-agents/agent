"""Claude's native plugin selection, refresh and inventory (Python 3.9+)."""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import functools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import agent_cli
from claude import sessions
from host import Host
import plugin_autoupdate
import plugin_refresh
import prune_plugin_cache
import required_plugins
import runtime_delivery
import session_relaunch
from tj_agent import content
from tj_agent.plugins import sources

HOST = Host.CLAUDE
CATALOG = ".claude-plugin/marketplace.json"
VALUE_OPTIONS = frozenset({
    "--model", "--permission-mode", "--settings", "--setting-sources", "--plugin-dir", "--plugin-url",
})
CONFIGURATION_OPTIONS = frozenset({"--settings", "--setting-sources", "--plugin-dir", "--plugin-url"})
FLAG_OPTIONS: frozenset[str] = frozenset()
DIRECTORY_OPTIONS: frozenset[str] = frozenset()
INFORMATION_OPTIONS = frozenset({"--help", "-h", "--version", "-v"})
MAINTENANCE_COMMANDS = frozenset({
    "plugin", "plugins", "auth", "doctor", "install", "setup-token", "update", "upgrade", "mcp",
})
GIT_TIMEOUT_SECONDS = 5
NODE_SHIM_BYPASSED = False


def resolve_executable() -> str:
    return agent_cli.resolve_claude_executable()


def known_marketplaces() -> dict:
    return plugin_refresh.read_json(HOST.configuration_root / "plugins/known_marketplaces.json")


def source_repository(source: dict) -> str | None:
    return plugin_refresh.repository(source.get("repo") if source.get("source") == "github" else source.get("url"))


def selection(executable: str, project: Path) -> tuple[dict[str, str | None], list[str]]:
    marketplaces, plugins = {}, {}
    for path in (HOST.configuration_root / "settings.json", project / ".claude/settings.json",
                 project / ".claude/settings.local.json"):
        settings = plugin_refresh.read_json(path)
        marketplaces.update(settings.get("extraKnownMarketplaces", {}))
        plugins.update(settings.get("enabledPlugins", {}))
    sources = {name: entry.get("source", {}) for name, entry in known_marketplaces().items()}
    sources.update({name: entry.get("source", {}) for name, entry in marketplaces.items()})
    return ({name: source_repository(source) for name, source in sources.items() if isinstance(source, dict)},
            [name for name, flag in plugins.items() if flag is True])


def selected(project: Path) -> tuple[dict[str, str], list[str]]:
    return plugin_refresh.selected(*selection("", project))


@functools.lru_cache(maxsize=None)
def git_common_directory(project: Path) -> str | None:
    if not project.is_dir() or not any((directory / ".git").exists() for directory in (project, *project.parents)):
        return None
    try:
        result = subprocess.run(['git', '-C', str(project), 'rev-parse', '--git-common-dir'],
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        raise required_plugins.RequiredPluginsError(
            f'Cannot resolve the native Claude repository for {project}: {error}') from error
    if result.returncode or not result.stdout.strip():
        raise required_plugins.RequiredPluginsError(
            f'Cannot resolve the native Claude repository for {project}: {result.stderr.strip()}')
    return os.path.normcase(str((project / result.stdout.strip()).resolve()))


def installations(project: Path, entries: list) -> list:
    if not entries:
        return []
    project = project.resolve()
    common = git_common_directory(project)
    selected, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        scope, owner = entry.get('scope'), entry.get('projectPath')
        if scope in {'project', 'local'}:
            if not isinstance(owner, str) or not owner:
                continue
            owner = Path(owner).resolve()
            try:
                owner_common = git_common_directory(owner)
            except required_plugins.RequiredPluginsError:
                continue
            if common and owner_common:
                if common != owner_common:
                    continue
            elif not project.is_relative_to(owner):
                continue
            key = entry.get('id'), scope
            if key in seen:
                continue
            seen.add(key)
        elif owner and scope not in {'user', 'managed'}:
            if not isinstance(owner, str) or not project.is_relative_to(Path(owner).resolve()):
                continue
        selected.append(entry)
    return selected


def location(entry: dict, project: Path) -> tuple[str, Path] | None:
    identity, path = entry.get("id"), entry.get("installPath")
    if not isinstance(identity, str) or not required_plugins.IDENTITY.fullmatch(identity) or not isinstance(path, str):
        return None
    return identity, Path(path)


def require_inventory_options(options: tuple[str, ...]) -> None:
    return None


def inventory_marketplaces(required: tuple[str, ...]) -> tuple[str | None, ...]:
    return (None,)


def inventory_entries(data: object) -> object:
    return data


def entry_errors(entries: list, project: Path) -> list[str]:
    return [f"{entry.get('id')} ({entry.get('scope')}): {error}"
            for entry in installations(project, entries)
            if entry.get("enabled") is True for error in entry.get("errors") or []]


def marketplace_location(executable: str, project: Path, name: str) -> str | None:
    return known_marketplaces().get(name, {}).get("installLocation")


def install_path(entry: dict, marketplace: str, plugin: str) -> Path:
    return Path(entry["installPath"])


def declared_scopes(identity: str, project: Path) -> list[str]:
    return [scope for scope, name in (("project", "settings.json"), ("local", "settings.local.json"))
            if plugin_refresh.read_json(project / ".claude" / name).get("enabledPlugins", {}).get(identity) is True]


def update_targets(identity: str, project: Path, inventory: list) -> list[tuple[str, Path]]:
    declared = declared_scopes(identity, project)
    targets = {}
    for entry in installations(project, inventory):
        scope = entry.get("scope")
        if entry.get("id") != identity or scope not in {"user", "project", "local"}:
            continue
        owner = project if scope == "user" else Path(entry["projectPath"])
        targets.setdefault(scope, (scope, owner))
    for scope in declared or ["user"]:
        targets.setdefault(scope, (scope, project))
    return [targets[key] for key in sorted(targets)]


def update_result(output: str, identity: str, scope: str) -> dict:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            break
    else:
        raise plugin_refresh.RefreshError(f"{identity} ({scope}): claude plugin update printed no result")
    outcome = value.get("updateOutcome")
    if outcome not in {"updated", "up_to_date"} or value.get("refreshFailed") or not value.get("newVersion"):
        raise plugin_refresh.RefreshError(f"{identity} ({scope}) was not updated: {value.get('message') or outcome}")
    return value


def entry_for(inventory: list, identity: str, scope: str, owner: Path) -> dict | None:
    return next((entry for entry in inventory if entry.get("id") == identity and entry.get("scope") == scope
                 and (scope == "user" or entry.get("projectPath")
                      and plugin_refresh.path_key(entry["projectPath"]) == plugin_refresh.path_key(owner))),
                None)


def persistence_problems(expected: list[tuple[str, str, Path, str]], inventory: list) -> list[str]:
    problems = []
    for identity, scope, owner, version in expected:
        entry = entry_for(inventory, identity, scope, owner)
        if entry is None or entry.get("version") != version:
            found = entry.get("version") if entry else "nothing"
            problems.append(f"{identity} ({scope}) update did not persist: installed_plugins.json has {found}, "
                            f"the update reported {version}")
    return problems


def dependency_identity(dependency: object, marketplace: str) -> str | None:
    if isinstance(dependency, dict):
        name, marketplace = dependency.get("name"), dependency.get("marketplace") or marketplace
    else:
        name = dependency
    if not isinstance(name, str) or not name or not isinstance(marketplace, str):
        return None
    return name if "@" in name else f"{name}@{marketplace}"


def catalog_dependencies() -> dict[str, list[str]]:
    result = {}
    for name, (_, location) in tj_agents_marketplaces().items():
        for entry in plugin_refresh.read_json(location / CATALOG).get("plugins", []):
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                result[f"{entry['name']}@{name}"] = [identity for dependency in entry.get("dependencies") or []
                                                     if (identity := dependency_identity(dependency, name))]
    return result


def missing_dependencies(identity: str, scope: str, owner: Path, dependencies: dict[str, list[str]],
                         inventory: list) -> list[str]:
    order, seen, visiting = [], set(), set()

    def visit(plugin: str) -> None:
        if plugin in visiting:
            raise plugin_refresh.RefreshError(f"Cyclic plugin dependency: {plugin}")
        visiting.add(plugin)
        for dependency in dependencies.get(plugin, []):
            if dependency not in seen:
                visit(dependency)
                seen.add(dependency)
                order.append(dependency)
        visiting.remove(plugin)
    visit(identity)
    if not order:
        return []
    effective = installations(owner, inventory)
    return [dependency for dependency in order if not any(
        entry.get("id") == dependency and (entry.get("scope") in {"user", "managed"}
                                           or scope != "user" and entry.get("scope") in {"project", "local"})
        for entry in effective)]


def dependency_installs(dependents: list[tuple[str, str, Path]]) -> list[tuple[str, str, Path]]:
    dependencies, inventory, installs = catalog_dependencies(), installed_entries(), []
    registered = known_marketplaces().keys()
    for identity, scope, owner in dependents:
        for dependency in missing_dependencies(identity, scope, owner, dependencies, inventory):
            install = (dependency, scope, owner)
            if dependency.rsplit("@", 1)[1] in registered and install not in installs:
                installs.append(install)
    return installs


def apply(executable: str, project: Path, installs: list[tuple[str, str]],
          updates: list[tuple[str, str, Path]], starting=None, dependencies=None) -> list[str]:
    run = plugin_refresh.run
    dependents = [(identity, scope, project) for identity, scope in installs] + updates
    dependencies = list(dict.fromkeys((dependencies or []) + dependency_installs(dependents)))
    for dependency, scope, owner in dependencies:
        run([executable, "plugin", "install", dependency, "--scope", scope], owner)
    for identity, scope in installs:
        if (identity, scope, project) not in dependencies:
            run([executable, "plugin", "install", identity, "--scope", scope], project)
    expected, updated = [], list(dict.fromkeys([identity for identity, _ in installs]
                                               + [dependency for dependency, _, _ in dependencies]))
    for identity, scope, owner in updates:
        if starting is not None:
            starting(identity, scope, owner)
        result = update_result(run([executable, "plugin", "update", identity, "--scope", scope, "--json"], owner),
                               identity, scope)
        expected.append((identity, scope, owner, result["newVersion"]))
        if result["updateOutcome"] == "updated" and identity not in updated:
            updated.append(identity)
    after = json.loads(run([executable, "plugin", "list", "--json"], project))
    problems = persistence_problems(expected, after) + entry_errors(after, project)
    for identity, scope, owner in list(dict.fromkeys(dependencies + [(identity, scope, project) for identity, scope in installs])):
        inventory = after if owner == project else json.loads(run([executable, "plugin", "list", "--json"], owner))
        entry = next((entry for entry in installations(owner, inventory)
                      if entry.get('id') == identity and entry.get('scope') == scope), None)
        if entry is None or entry.get("enabled") is not True:
            problems.append(f"{identity} ({scope}): native installation did not persist enabled")
    if problems:
        raise plugin_refresh.RefreshError("; ".join(problems))
    return updated


def refresh_marketplaces(executable: str, project: Path, marketplaces: dict) -> None:
    known = known_marketplaces()
    for name in marketplaces:
        if name not in known:
            plugin_refresh.run([executable, "plugin", "marketplace", "add", marketplaces[name]], project)
        plugin_refresh.run([executable, "plugin", "marketplace", "update", name], project)


def repository_key(project: Path) -> str:
    return git_common_directory(project) or plugin_refresh.path_key(project)


def repository_projects(projects: list[Path]) -> list[Path]:
    discovered, seen = set(projects), set()
    for project in projects:
        key = repository_key(project)
        if key in seen or not project.is_dir():
            continue
        seen.add(key)
        result = subprocess.run(["git", "-C", str(project), "worktree", "list", "--porcelain", "-z"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                                creationflags=plugin_autoupdate.NO_WINDOW)
        if result.returncode == 0:
            discovered.update(Path(field.removeprefix("worktree ")) for field in result.stdout.split("\0")
                              if field.startswith("worktree "))
    return sorted(path.resolve() for path in discovered if path.is_dir())


def materialize(root: Path, tree: dict[str, bytes], modes: dict[str, int] | None = None) -> Path:
    if any(runtime_delivery.is_reparse(path) for path in (root, *root.parents) if path.exists()):
        raise plugin_refresh.RefreshError(f"Plugin cache traverses a link: {root}")
    root.mkdir(parents=True, exist_ok=True)
    modes = modes or {}
    destination = root / content.digest({**tree, "\0modes": json.dumps(modes, sort_keys=True).encode()})
    if os.path.lexists(destination) and runtime_delivery.is_reparse(destination):
        raise plugin_refresh.RefreshError(f"Plugin cache destination is a link: {destination}")
    if (destination.is_dir() and plugin_refresh.package_tree(destination) == tree
            and all((destination / name).stat().st_mode & 0o777 == mode for name, mode in modes.items())):
        return destination
    with tempfile.TemporaryDirectory(prefix="stage-", dir=root) as temporary:
        stage = Path(temporary) / "package"
        stage.mkdir()
        for name, body in tree.items():
            path = stage / name
            if not path.resolve().is_relative_to(stage.resolve()):
                raise plugin_refresh.RefreshError(f"Plugin file escapes package: {name}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            if name in modes:
                path.chmod(modes[name])
        if destination.exists():
            destination = root / (destination.name + "-" + Path(temporary).name)
        stage.rename(destination)
    return destination


def synchronize(projects: list[Path], *, identities: set[str] | None = None, force: bool = False) -> list[str]:
    path = HOST.configuration_root / "plugins/installed_plugins.json"
    lock = plugin_autoupdate.acquire_lock(path.with_suffix(".lock"))
    if lock is None:
        raise plugin_refresh.RefreshError("Claude plugin registrations are already being updated")
    try:
        original = path.read_bytes() if path.is_file() else None
        document = plugin_refresh.read_json(path) if original is not None else {"version": 2, "plugins": {}}
        if not isinstance(document.get("plugins"), dict):
            raise plugin_refresh.RefreshError("Claude plugin registry has no plugins object")
        wanted, owners, settings_targets = {}, {}, {}
        projects = repository_projects(projects)
        for project in projects:
            project = project.resolve()
            if not project.is_dir():
                continue
            key = repository_key(project)
            owners.setdefault(key, project)
            _, enabled = selected(project)
            for identity in enabled:
                if not required_plugins.IDENTITY.fullmatch(identity):
                    raise plugin_refresh.RefreshError(f"Invalid plugin identity: {identity}")
                if identities is not None and identity not in identities:
                    continue
                for scope in declared_scopes(identity, project):
                    wanted.setdefault(identity, set()).add((scope, key))
                    settings_targets.setdefault(identity, set()).add((scope, project))
        catalogs = {name: (location, {entry["name"]: entry for entry in plugin_refresh.read_json(
            location / CATALOG).get("plugins", []) if isinstance(entry, dict) and isinstance(entry.get("name"), str)})
            for name, (_, location) in tj_agents_marketplaces().items()}
        contents = plugin_autoupdate.read_contents()
        pending = list(wanted)
        dependencies = {f"{plugin}@{marketplace}": [dependency_identity(value, marketplace)
                         for value in entry.get("dependencies") or []]
                        for marketplace, (_, catalog) in catalogs.items() for plugin, entry in catalog.items()}
        for identity in wanted:
            missing_dependencies(identity, "project", projects[0], dependencies, [])
        while pending:
            identity = pending.pop()
            plugin, _, marketplace = identity.partition("@")
            entry = catalogs.get(marketplace, (None, {}))[1].get(plugin, {})
            for value in entry.get("dependencies") or []:
                dependency = dependency_identity(value, marketplace)
                if dependency is None or not required_plugins.IDENTITY.fullmatch(dependency) or dependency == identity:
                    raise plugin_refresh.RefreshError(f"Invalid dependency of {identity}")
                added = wanted[identity] - wanted.get(dependency, set())
                extra = settings_targets[identity] - settings_targets.get(dependency, set())
                if added or extra:
                    wanted.setdefault(dependency, set()).update(added)
                    settings_targets.setdefault(dependency, set()).update(extra)
                    pending.append(dependency)
        unknown = {identity for identity in wanted if identity.rsplit("@", 1)[1] not in catalogs}
        if unknown:
            raise plugin_refresh.RefreshError("Dependency marketplace is unavailable: " + ", ".join(sorted(unknown)))
        settings = {}
        for identity, targets in settings_targets.items():
            for scope, project in targets:
                target = project / ".claude" / ("settings.local.json" if scope == "local" else "settings.json")
                if target not in settings:
                    settings[target] = (target.read_bytes() if target.is_file() else None, plugin_refresh.read_json(target))
                settings[target][1].setdefault("enabledPlugins", {})[identity] = True
        publishers, payloads, trees, changed = {}, {}, {}, set()
        with tempfile.TemporaryDirectory(prefix="tj-agents-packages-") as temporary:
            for identity in sorted(set(document["plugins"]) | set(wanted)):
                plugin, _, marketplace = identity.partition("@")
                if marketplace not in catalogs or not required_plugins.IDENTITY.fullmatch(identity):
                    continue
                rows = document["plugins"].get(identity, [])
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise plugin_refresh.RefreshError(f"Invalid Claude registrations for {identity}")
                kept, groups = [], {}
                for row in rows:
                    scope, owner = row.get("scope"), row.get("projectPath")
                    if scope not in {"project", "local"}:
                        kept.append(row)
                        continue
                    if not isinstance(owner, str) or not Path(owner).is_dir():
                        continue
                    key = repository_key(Path(owner))
                    if key not in owners:
                        kept.append(row)
                        continue
                    groups.setdefault((scope, key), row)
                location, catalog = catalogs[marketplace]
                entry = catalog.get(plugin)
                for group in sorted(set(groups) | wanted.get(identity, set())):
                    row = groups.get(group)
                    if group not in wanted.get(identity, set()):
                        if row is not None:
                            kept.append(row)
                        continue
                    if entry is None:
                        raise plugin_refresh.RefreshError(f"Selected plugin is not published: {identity}")
                    source = entry.get("source")
                    publisher = (plugin_autoupdate.publisher_key(source.get("url"), source.get("sha") or source.get("ref"))
                                 if isinstance(source, dict) and source.get("source") == "git-subdir" else None)
                    head = contents["heads"].get(publisher) if publisher else None
                    modes, local_tree = {}, None
                    if not head:
                        if identity not in trees:
                            trees[identity] = (sources.tree(
                                source, location, owners[group[1]], Path(temporary), publishers, modes=modes), modes)
                        local_tree, modes = trees[identity]
                        resolved = sources.resolved_head(source, publishers) if publisher else None
                        if resolved:
                            head = resolved
                            contents["heads"][publisher] = head
                            plugin_autoupdate.record_contents(heads={publisher: head})
                    revision = json.dumps([source, head, content.digest(local_tree) if publisher is None else None,
                                           modes if publisher is None else None], sort_keys=True)
                    current = Path(row["installPath"]) if row and isinstance(row.get("installPath"), str) else None
                    if (not force and row and row.get("tjAgentsRevision") == revision and current and current.is_dir()
                            and row.get("tjAgentsDigest") == content.digest(plugin_refresh.package_tree(current))
                            and all((current / name).stat().st_mode & 0o777 == mode
                                    for name, mode in row.get("tjAgentsModes", {}).items())):
                        kept.append(row)
                        continue
                    if identity not in payloads:
                        frozen = {**source, "sha": head} if publisher and head else source
                        tree = (local_tree if local_tree is not None else sources.tree(
                            frozen, location, owners[group[1]], Path(temporary), publishers, modes=modes))
                        manifest = json.loads(tree.get(".claude-plugin/plugin.json", b"{}"))
                        if manifest.get("name") != plugin:
                            raise plugin_refresh.RefreshError(f"Published plugin manifest does not name {identity}")
                        installed = materialize(HOST.configuration_root / "plugins/cache" / marketplace / plugin, tree, modes)
                        payloads[identity] = (installed, tree, manifest, modes)
                    installed, tree, manifest, modes = payloads[identity]
                    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    record = {**(row or {}), "scope": group[0], "projectPath": str(owners[group[1]]),
                              "installPath": str(installed), "version": manifest.get("version") or installed.name,
                              "installedAt": (row or {}).get("installedAt", now), "lastUpdated": now,
                              "tjAgentsRevision": revision, "tjAgentsDigest": content.digest(tree), "tjAgentsModes": modes}
                    if head:
                        record["gitCommitSha"] = head
                    kept.append(record)
                    if row != record:
                        changed.add(identity)
                if kept:
                    document["plugins"][identity] = kept
                else:
                    document["plugins"].pop(identity, None)
        if (path.read_bytes() if path.is_file() else None) != original:
            raise plugin_refresh.RefreshError("Claude changed plugin registrations during refresh; retry")
        for target, (before, _) in settings.items():
            if (target.read_bytes() if target.is_file() else None) != before:
                raise plugin_refresh.RefreshError(f"Claude plugin selection changed during refresh: {target}; retry")
        for target, (before, selection) in settings.items():
            prior = json.loads(before.decode("utf-8-sig")) if before is not None else {}
            if prior != selection:
                plugin_autoupdate.write_json(target, selection)
        encoded = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if encoded != original:
            plugin_autoupdate.write_json(path, document)
        return sorted(changed)
    finally:
        lock.close()


def refresh_plugins(executable: str, project: Path, marketplaces: dict, plugins: list[str]) -> list[str]:
    retired = retire_unpublished_user_plugins(marketplaces)
    retired.extend(uninstall_unpublished(executable, marketplaces))
    plugins = [identity for identity in plugins if identity not in retired or declared_scopes(identity, project)]
    if not plugins:
        return []
    scoped = {identity for identity in plugins if declared_scopes(identity, project)}
    if scoped:
        refresh_heads(None)
    changed = synchronize([project], identities=scoped, force=True) if scoped else []
    plugins = [identity for identity in plugins if identity not in scoped]
    if not plugins:
        return changed
    inventory = json.loads(plugin_refresh.run([executable, "plugin", "list", "--json"], project))
    effective = installations(project, inventory)
    installs = [(identity, scope) for identity in plugins for scope in declared_scopes(identity, project)
                if not any(entry.get('id') == identity and entry.get('scope') == scope for entry in effective)]
    updates = [(identity, scope, owner) for identity in plugins
               for scope, owner in update_targets(identity, project, inventory)]
    return sorted(set(changed + apply(executable, project, installs, updates)))


def install_missing(executable: str, project: Path, missing) -> None:
    installs = [(identity, scope) for identity in missing
                for scope in declared_scopes(identity, project) or ["user"]]
    apply(executable, project, installs, [])


def installed_entries() -> list[dict]:
    plugins = plugin_refresh.read_json(HOST.configuration_root / "plugins/installed_plugins.json").get("plugins")
    if not isinstance(plugins, dict):
        return []
    return [{**entry, "id": identity} for identity, entries in plugins.items() if isinstance(entries, list)
            for entry in entries if isinstance(entry, dict)]


def tj_agents_marketplaces() -> dict[str, tuple[str, Path]]:
    found = {}
    for name, entry in known_marketplaces().items():
        source = entry.get("source") if isinstance(entry, dict) else None
        if not isinstance(source, dict) or not isinstance(entry.get("installLocation"), str):
            continue
        repo = source_repository(source)
        if plugin_refresh.tj_agents(repo):
            found[name] = (repo, Path(entry["installLocation"]))
    return found


def published_plugins() -> dict[str, set[str]]:
    published = {}
    for name, (_, location) in tj_agents_marketplaces().items():
        names = {entry["name"] for entry in plugin_refresh.read_json(location / CATALOG).get("plugins", [])
                 if isinstance(entry, dict) and isinstance(entry.get("name"), str)}
        if names:
            published[name] = names
    return published


def retire_unpublished_user_plugins(refreshed) -> list[str]:
    path = HOST.configuration_root / "settings.json"
    settings = plugin_refresh.read_json(path)
    enabled = settings.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return []
    published = {name: names for name, names in published_plugins().items() if name in refreshed}
    retired = sorted(identity for identity in enabled if identity.count("@") == 1
                     and identity.split("@")[1] in published and identity.split("@")[0] not in published[identity.split("@")[1]])
    if retired:
        settings["enabledPlugins"] = {identity: flag for identity, flag in enabled.items() if identity not in retired}
        plugin_autoupdate.write_json(path, settings)
        print("plugins: removed user-scope selections no longer published: " + ", ".join(retired), file=sys.stderr)
    return retired


def uninstall_unpublished(executable: str, refreshed) -> list[str]:
    published = {name: names for name, names in published_plugins().items() if name in refreshed}
    removed = []
    for entry in installed_entries():
        identity, scope = entry["id"], entry.get("scope")
        plugin, _, marketplace = identity.partition("@")
        if marketplace not in published or plugin in published[marketplace]:
            continue
        owner = Path(entry.get("projectPath") or Path.home())
        if scope not in {"user", "project", "local"} or not owner.is_dir():
            continue
        plugin_refresh.run([executable, "plugin", "uninstall", identity, "--scope", scope], owner)
        removed.append(identity)
    return removed


def catalog_publishers(marketplaces: dict[str, tuple[str, Path]]) -> dict[str, dict[str, tuple[str, str]]]:
    result = {}
    for name, (_, location) in marketplaces.items():
        result[name] = {}
        for entry in plugin_refresh.read_json(location / CATALOG).get("plugins", []):
            source = entry.get("source") if isinstance(entry, dict) else None
            if isinstance(source, dict) and source.get("source") == "git-subdir":
                key = plugin_autoupdate.publisher_key(source.get("url"), source.get("sha") or source.get("ref"))
                if key:
                    result[name][entry.get("name")] = (key, plugin_autoupdate.plugin_path(source))
    return result


def refresh_heads(repositories: set[str] | None, revision=plugin_autoupdate.remote_revision, query=None) -> set[str]:
    marketplaces = tj_agents_marketplaces()
    owners = {f"{plugin}@{name}": source for name, plugins in catalog_publishers(marketplaces).items()
              for plugin, source in plugins.items()}
    everything = (repositories is None or not plugin_autoupdate.read_contents()["heads"]
                  or any(repo in repositories for repo, _ in marketplaces.values()))
    wanted = {}
    for key, path in owners.values():
        if everything or key.rsplit("@", 1)[0] in repositories:
            wanted.setdefault(key, set()).add(path)
    if not wanted:
        return set()
    installed = {}
    for entry in installed_entries():
        sha = entry.get("gitCommitSha")
        if isinstance(sha, str) and plugin_autoupdate.SHA.fullmatch(sha) and entry["id"] in owners:
            installed.setdefault(owners[entry["id"]][0], set()).add(sha)
    heads = plugin_autoupdate.refresh_contents(wanted, installed, query, revision)
    return {key for key in wanted if key not in heads}


def attempt_key(identity: str, scope: str, owner: Path) -> str:
    return f"{identity}|{scope}|{'user' if scope == 'user' else plugin_refresh.path_key(owner)}"


def reconcile(project: Path, event: bool = False, repositories: set[str] | None = None,
              fresh: frozenset[str] = frozenset()) -> list[str]:
    retire_unpublished_user_plugins(fresh)
    marketplaces, plugins = selected(project)
    scoped = {identity for identity in plugins if declared_scopes(identity, project)}
    changed = synchronize([project], identities=scoped) if scoped else []
    plugins = [identity for identity in plugins if identity not in scoped]
    inventory = installed_entries()
    effective = installations(project, inventory)
    contents = plugin_autoupdate.read_contents()
    publishers = catalog_publishers(tj_agents_marketplaces())
    installs, updates, present, attempts = [], [], [], {}
    for identity in plugins:
        plugin, name = identity.rsplit("@", 1)
        key, path = publishers.get(name, {}).get(plugin, (None, None))
        head = contents["heads"].get(key) if key else None
        forced = event and key is None and (repositories is None or marketplaces.get(name) in repositories)
        installs += [(identity, scope) for scope in declared_scopes(identity, project) or ["user"]
                     if not any(entry.get('id') == identity and entry.get('scope') == scope for entry in effective)]
        for scope, owner in update_targets(identity, project, inventory):
            entry = entry_for(inventory, identity, scope, owner)
            if entry is None:
                continue
            present.append((identity, scope, owner))
            same = (plugin_autoupdate.same_content(contents, key.rsplit("@", 1)[0], path, entry.get("gitCommitSha"), head)
                    if head is not None else True)
            attempted = contents["attempts"].get(attempt_key(identity, scope, owner)) == head
            if forced or (same is not True if event else same is False) and not attempted:
                updates.append((identity, scope, owner))
                if head is not None:
                    attempts[attempt_key(identity, scope, owner)] = head
    dependencies = dependency_installs(present)
    if not installs and not updates and not dependencies:
        return changed
    touched = {work[0].rsplit("@", 1)[1] for work in installs + updates + dependencies}
    native = resolve_executable()
    refresh_marketplaces(native, project, {name: repo for name, repo in marketplaces.items() if name in touched - fresh})

    def starting(identity: str, scope: str, owner: Path) -> None:
        if (key := attempt_key(identity, scope, owner)) in attempts:
            with contextlib.suppress(OSError):
                plugin_autoupdate.record_contents(attempts={key: attempts[key]})

    return sorted(set(changed + apply(native, project, installs, updates, starting, dependencies)))



def update(repositories: set[str] | None, project: Path, updated: set[str] | None = None,
           revision=plugin_autoupdate.remote_revision, query=None) -> bool:
    marketplaces, _ = selected(project)
    fresh = {name: repo for name, repo in marketplaces.items() if repositories is None or repo in repositories}
    if fresh:
        refresh_marketplaces(resolve_executable(), project, fresh)
    unresolved = refresh_heads(repositories, revision, query)
    if unresolved:
        print(f"plugins: could not read the published head of {', '.join(sorted(unresolved))}", file=sys.stderr)
    changed = reconcile(project, event=True, repositories=repositories, fresh=frozenset(fresh))
    if updated is not None:
        updated.update(changed)
    return repositories is None or not any(key.rsplit("@", 1)[0] in repositories for key in unresolved)


def update_registered(repositories: set[str] | None = None) -> list[str]:
    projects = sorted({Path(entry["projectPath"]).resolve() for entry in installed_entries()
                       if entry.get("scope") in {"project", "local"} and isinstance(entry.get("projectPath"), str)
                       and Path(entry["projectPath"]).is_dir()})
    if not projects:
        return synchronize([])
    marketplaces = {name: repo for name, (repo, _) in tj_agents_marketplaces().items()}
    refresh_marketplaces(resolve_executable(), projects[0], marketplaces)
    unresolved = refresh_heads(repositories)
    if unresolved:
        raise plugin_refresh.RefreshError("Could not resolve plugin publishers: " + ", ".join(sorted(unresolved)))
    changed = synchronize(projects)
    if prune_plugin_cache.main(["--apply"]):
        raise plugin_refresh.RefreshError("Claude plugin cache reconciliation failed")
    return changed


def ensure_hooks(project: Path, executable: str | None = None, *, running_session: bool = False) -> str | None:
    return None


def effective_installation(entries: list, project: Path) -> dict | None:
    ranked = []
    for entry in installations(project, entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("installPath"), str):
            continue
        scope, owner = entry.get("scope"), entry.get("projectPath")
        if scope in {"local", "project"} and owner:
            ranked.append(((0 if scope == "local" else 1), -len(Path(owner).resolve().parts), entry))
        elif scope in {"user", "managed"}:
            ranked.append((2 if scope == "user" else 3, 0, entry))
    return min(ranked, key=lambda item: item[:2])[2] if ranked else None


def stale_plugins(pid: int, project: Path, identities: list[str], installed: dict, updated: set[str]) -> list[str]:
    stale = []
    for identity in identities:
        entry = effective_installation(installed.get(identity) or [], project)
        if entry is None:
            continue
        current = Path(entry["installPath"])
        loaded = {os.path.normcase(str(version)) for version in (current.parent.iterdir() if pid and current.parent.is_dir() else ())
                  if (version / plugin_refresh.IN_USE / str(pid)).is_file()}
        if (os.path.normcase(str(current)) not in loaded) and (loaded or identity in updated):
            stale.append(identity)
    return stale


def adopt(project: Path, updated: set[str], reason: str | None, allowed: bool) -> dict | None:
    identity = sessions.session_identity()
    if identity is None:
        return None
    installed = plugin_refresh.read_json(HOST.configuration_root / "plugins/installed_plugins.json").get("plugins") or {}
    _, plugins = selected(project)
    stale = stale_plugins(identity[1], project, plugins, installed, updated)
    if not stale:
        return None
    if not allowed:
        return {"relaunch": "failed", "stale": stale, "error": "--no-relaunch prevents this session adopting the installed plugins"}
    try:
        result = session_relaunch.relaunch(project, installed, host=HOST)
    except (OSError, RuntimeError, ValueError) as error:
        return {"relaunch": "failed", "stale": stale, "error": str(error)}
    if result.get("relaunch") != "armed":
        return {"relaunch": "failed", "stale": stale, "error": "the owning Claude client restart was not armed"}
    return {**result, "stale": stale, "adoption": "pending"}
