"""Converge Claude's repository registrations in one transaction (Python 3.11+)."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

import plugin_autoupdate
import plugin_refresh
import required_plugins
import runtime_delivery


def repository_key(project: Path) -> str:
    from claude import plugins

    return plugins.git_common_directory(project) or plugin_refresh.path_key(project)


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


def digest(tree: dict[str, bytes]) -> str:
    result = hashlib.sha256()
    for name, body in sorted(tree.items()):
        result.update(name.encode("utf-8") + b"\0" + str(len(body)).encode("ascii") + b"\0" + body)
    return result.hexdigest()


def materialize(root: Path, tree: dict[str, bytes], modes: dict[str, int] | None = None) -> Path:
    if any(runtime_delivery.is_reparse(path) for path in (root, *root.parents) if path.exists()):
        raise plugin_refresh.RefreshError(f"Plugin cache traverses a link: {root}")
    root.mkdir(parents=True, exist_ok=True)
    modes = modes or {}
    destination = root / digest({**tree, "\0modes": json.dumps(modes, sort_keys=True).encode()})
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
    from claude import plugins
    import reload_plugins

    path = plugins.HOST.configuration_root / "plugins/installed_plugins.json"
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
            _, selected = plugins.selected(project)
            for identity in selected:
                if not required_plugins.IDENTITY.fullmatch(identity):
                    raise plugin_refresh.RefreshError(f"Invalid plugin identity: {identity}")
                if identities is not None and identity not in identities:
                    continue
                for scope in plugins.declared_scopes(identity, project):
                    wanted.setdefault(identity, set()).add((scope, key))
                    settings_targets.setdefault(identity, set()).add((scope, project))
        catalogs = {name: (location, {entry["name"]: entry for entry in plugin_refresh.read_json(
            location / plugins.CATALOG).get("plugins", []) if isinstance(entry, dict) and isinstance(entry.get("name"), str)})
            for name, (_, location) in plugins.tj_agents_marketplaces().items()}
        contents = plugin_autoupdate.read_contents()
        pending = list(wanted)
        dependencies = {f"{plugin}@{marketplace}": [plugins.dependency_identity(value, marketplace)
                         for value in entry.get("dependencies") or []]
                        for marketplace, (_, catalog) in catalogs.items() for plugin, entry in catalog.items()}
        for identity in wanted:
            plugins.missing_dependencies(identity, "project", projects[0], dependencies, [])
        while pending:
            identity = pending.pop()
            plugin, _, marketplace = identity.partition("@")
            entry = catalogs.get(marketplace, (None, {}))[1].get(plugin, {})
            for value in entry.get("dependencies") or []:
                dependency = plugins.dependency_identity(value, marketplace)
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
                            trees[identity] = (reload_plugins.source_tree(
                                source, location, owners[group[1]], Path(temporary), publishers, modes=modes), modes)
                        local_tree, modes = trees[identity]
                    revision = json.dumps([source, head, digest(local_tree) if local_tree is not None else None], sort_keys=True)
                    current = Path(row["installPath"]) if row and isinstance(row.get("installPath"), str) else None
                    if (not force and row and row.get("tjAgentsRevision") == revision and current and current.is_dir()
                            and row.get("tjAgentsDigest") == digest(plugin_refresh.package_tree(current))):
                        kept.append(row)
                        continue
                    if identity not in payloads:
                        frozen = {**source, "sha": head} if publisher and head else source
                        tree = (local_tree if local_tree is not None else reload_plugins.source_tree(
                            frozen, location, owners[group[1]], Path(temporary), publishers, modes=modes))
                        manifest = json.loads(tree.get(".claude-plugin/plugin.json", b"{}"))
                        if manifest.get("name") != plugin:
                            raise plugin_refresh.RefreshError(f"Published plugin manifest does not name {identity}")
                        installed = materialize(plugins.HOST.configuration_root / "plugins/cache" / marketplace / plugin, tree, modes)
                        payloads[identity] = (installed, tree, manifest)
                    installed, tree, manifest = payloads[identity]
                    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    record = {**(row or {}), "scope": group[0], "projectPath": str(owners[group[1]]),
                              "installPath": str(installed), "version": manifest.get("version") or installed.name,
                              "installedAt": (row or {}).get("installedAt", now), "lastUpdated": now,
                              "tjAgentsRevision": revision, "tjAgentsDigest": digest(tree)}
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
