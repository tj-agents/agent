#!/usr/bin/env python3
"""Create or extend a repository's plugin selection and generate host settings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

import lock_io
import plugin_selection
import repo_config


CATALOG = Path(__file__).resolve().parent / "marketplace_catalog.lock.json"
STACKS = Path(__file__).resolve().parent / "plugin_stacks.json"


def load_roster() -> dict:
    snapshot = lock_io.load_json(CATALOG)
    revision = snapshot.get("revision")
    if (set(snapshot) != {"repository", "revision", "catalog"}
            or snapshot["repository"] != plugin_selection.MARKETPLACE_REPOSITORY
            or not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None):
        raise lock_io.LockError(f"{CATALOG}: invalid marketplace snapshot")
    catalog = snapshot["catalog"]
    if not isinstance(catalog, dict) or catalog.get("name") != plugin_selection.MARKETPLACE_NAME:
        raise lock_io.LockError(f"{CATALOG}: invalid marketplace catalog")
    entries = catalog.get("plugins")
    if not isinstance(entries, list):
        raise lock_io.LockError(f"{CATALOG}: plugins must be a list")
    plugins = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise lock_io.LockError(f"{CATALOG}: invalid plugin entry")
        name = entry["name"]
        if name in plugins:
            raise lock_io.LockError(f"{CATALOG}: duplicate plugin: {name}")
        plugins[name] = lock_io.require_string_list(entry.get("requires", []), f"{name}.requires")
    for name, required in plugins.items():
        if any(dependency not in plugins for dependency in required):
            raise lock_io.LockError(f"{CATALOG}: unknown dependency in {name}")
    stacks = lock_io.load_json(STACKS)
    if stacks.get("schema_version") != 1 or set(stacks) != {"schema_version", "stacks"} or not isinstance(stacks["stacks"], dict):
        raise lock_io.LockError(f"{STACKS}: invalid stack choices")
    for stack, selected in stacks["stacks"].items():
        lock_io.require_string_list(selected, f"{stack}.plugins")
        if any(name not in plugins for name in selected):
            raise lock_io.LockError(f"{STACKS}: unknown plugin in stack {stack}")
    return {"plugins": plugins, "stacks": stacks["stacks"]}


def expand(requested: set[str], roster: dict) -> list[str]:
    known = roster["plugins"]
    pending = sorted(requested)
    selected: set[str] = set()
    while pending:
        name = pending.pop()
        if name not in known:
            raise lock_io.LockError(f"Unknown plugin: {name}")
        if name not in selected:
            selected.add(name)
            pending.extend(known[name])
    return sorted(selected)


def requested_plugins(stacks: list[str], plugins: list[str], roster: dict) -> tuple[set[str], set[str]]:
    unknown = sorted(set(stacks) - set(roster["stacks"]))
    if unknown:
        raise lock_io.LockError("Unknown stack: " + ", ".join(unknown) + "; choose from " + ", ".join(sorted(roster["stacks"])))
    names = set().union(*(roster["stacks"][stack] for stack in stacks))
    foreign = set()
    for plugin in plugins:
        identity = plugin if "@" in plugin else plugin_selection.plugin_id(plugin)
        if not plugin_selection.valid_identity(identity):
            raise lock_io.LockError(f"--plugin must be <plugin> or <plugin>@<marketplace>: {plugin}")
        name, marketplace = plugin_selection.split(identity)
        if marketplace == plugin_selection.MARKETPLACE_NAME:
            names.add(name)
        else:
            foreign.add(identity)
    return names, foreign


def parse_marketplace(value: str) -> tuple[str, dict]:
    name, separator, repository = value.partition("=")
    if not separator:
        raise lock_io.LockError(f"--marketplace must be <name>=<owner>/<repository>: {value}")
    source = {"repository": repository}
    plugin_selection.validate_marketplace(name, source)
    return name, source


def removal_identities(plugins: list[str]) -> set[str]:
    identities = set()
    for plugin in plugins:
        identity = plugin if "@" in plugin else plugin_selection.plugin_id(plugin)
        if not plugin_selection.valid_identity(identity):
            raise lock_io.LockError(f"--remove-plugin must be <plugin> or <plugin>@<marketplace>: {plugin}")
        identities.add(identity)
    return identities


def selection_document(root: Path, requested: set[str], foreign: set[str], removals: set[str], declared: dict, roster: dict) -> dict:
    selection = plugin_selection.resolve(root)
    kept: list[str] = []
    marketplaces = {}
    if selection.is_file():
        document = lock_io.load_json(selection)
        marketplaces, kept = plugin_selection.validate(document)
        kept = [identity for identity in kept if identity not in removals]
        requested |= {plugin_selection.split(identity)[0] for identity in kept
                      if plugin_selection.split(identity)[1] == plugin_selection.MARKETPLACE_NAME
                      and plugin_selection.split(identity)[0] in roster["plugins"]}
    elif not requested and not foreign and not removals:
        requested = set(plugin_selection.CORE_PLUGINS)
    selected = {plugin_selection.plugin_id(name) for name in expand(requested, roster)} | set(kept) | foreign
    selected -= removals
    local = {plugin_selection.split(identity)[0] for identity in selected
             if plugin_selection.split(identity)[1] == plugin_selection.MARKETPLACE_NAME
             and plugin_selection.split(identity)[0] in roster["plugins"]}
    required = set().union(*(expand({name}, roster) for name in local)) if local else set()
    blocked = sorted(name for name in required if plugin_selection.plugin_id(name) in removals)
    if blocked:
        raise lock_io.LockError("Cannot remove prerequisite required by remaining plugins: " + ", ".join(blocked))
    if requested:
        marketplaces.setdefault(plugin_selection.MARKETPLACE_NAME, {"repository": plugin_selection.MARKETPLACE_REPOSITORY})
    marketplaces.update(declared)
    used_marketplaces = {plugin_selection.split(identity)[1] for identity in selected}
    marketplaces = {name: source for name, source in marketplaces.items() if name in used_marketplaces}
    document = {
        "schema_version": plugin_selection.SCHEMA_VERSION,
        "marketplaces": dict(sorted(marketplaces.items())),
        "plugins": sorted(selected),
    }
    plugin_selection.validate(document)
    return document


def plan(root: Path, stacks: list[str], plugins: list[str], removals: list[str], scope: str | None, marketplaces: list[str] = ()) -> tuple[dict[Path, str | None], Path]:
    root = root.expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise lock_io.LockError(f"Repository root is not a directory: {root}")
    roster = load_roster()
    names, foreign = requested_plugins(stacks, plugins, roster)
    declared = {}
    for value in marketplaces:
        name, source = parse_marketplace(value)
        if name in declared:
            raise lock_io.LockError(f"--marketplace {name} is given more than once")
        declared[name] = source
    document = selection_document(root, names, foreign, removal_identities(removals), declared, roster)
    selection = root / ".agents" / plugin_selection.SELECTION_NAME
    host_targets = repo_config.plan(selection, root=root, scope=scope, prospective_paths=[selection], lock_document=document)
    targets = {selection: json.dumps(document, indent=2, ensure_ascii=False) + "\n", **host_targets}
    return targets, root


def run(root: Path, stacks: list[str], plugins: list[str], mode: str, scope: str | None = None, marketplaces: list[str] = (), removals: list[str] = ()) -> list[str]:
    targets, root = plan(root, stacks, plugins, removals, scope, marketplaces)
    changed = {path: value for path, value in targets.items() if repo_config.text_at(path) != value}
    drift = [path.relative_to(root).as_posix() for path in changed]
    if mode == "write":
        repo_config.write_batch(changed)
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--marketplace", action="append", default=[], metavar="NAME=OWNER/REPO")
    parser.add_argument("--stack", action="append", default=[])
    parser.add_argument("--plugin", action="append", default=[])
    parser.add_argument("--remove-plugin", action="append", default=[])
    parser.add_argument("--scope", choices=("project", "local"))
    parser.add_argument("--mode", required=True, choices=("preview", "check", "write"))
    arguments = parser.parse_args(argv)
    try:
        drift = run(arguments.root, arguments.stack, arguments.plugin, arguments.mode, arguments.scope, arguments.marketplace, arguments.remove_plugin)
    except (lock_io.LockError, OSError, UnicodeError, ValueError) as error:
        print(f"Plugin adoption failed: {error}", file=sys.stderr)
        return 1
    if drift:
        prefix = "Updated" if arguments.mode == "write" else "Would update" if arguments.mode == "preview" else "Drift in"
        print(prefix + ": " + ", ".join(drift))
    return int(arguments.mode == "check" and bool(drift))


if __name__ == "__main__":
    raise SystemExit(main())
