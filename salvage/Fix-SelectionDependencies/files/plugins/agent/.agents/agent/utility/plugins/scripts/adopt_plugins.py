#!/usr/bin/env python3
"""Create or extend a repository's plugin selection and generate host settings."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

import lock_io
import plugin_selection
import published_harness
import repo_config
from plugin_runtime import declarations, process

LOCK = Path(__file__).resolve().parent / "marketplace_plugins.lock.json"
STACKS = Path(__file__).resolve().parent / "plugin_stacks.json"
NO_ENVIRONMENT: Mapping[str, str] = MappingProxyType({})


def load_roster(snapshot: dict | None = None) -> dict:
    live = snapshot is not None
    snapshot = lock_io.load_json(LOCK) if snapshot is None else snapshot
    revision = snapshot.get("revision")
    if (set(snapshot) != {"repository", "revision", "marketplace"}
            or snapshot["repository"] != plugin_selection.MARKETPLACE_REPOSITORY
            or not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None):
        raise lock_io.LockError(f"{LOCK}: invalid marketplace snapshot")
    marketplace = snapshot["marketplace"]
    if not isinstance(marketplace, dict) or marketplace.get("name") != plugin_selection.MARKETPLACE_NAME:
        raise lock_io.LockError(f"{LOCK}: invalid marketplace plugins document")
    entries = marketplace.get("plugins")
    if not isinstance(entries, list):
        raise lock_io.LockError(f"{LOCK}: plugins must be a list")
    plugins: set[str] = set()
    profiles = set()
    organizations = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise lock_io.LockError(f"{LOCK}: invalid plugin entry")
        name = entry["name"]
        if name in plugins:
            raise lock_io.LockError(f"{LOCK}: duplicate plugin: {name}")
        plugins.add(name)
        if entry.get("kind") == "profile":
            profiles.add(name)
        elif entry.get("kind") == "organization":
            if not isinstance(entry.get("extends"), str):
                raise lock_io.LockError(f"{LOCK}: organization {name} must extend a profile")
            organizations[name] = entry["extends"]
    if live:
        return {"plugins": plugins, "profiles": profiles, "organizations": organizations, "stacks": {}}
    stacks = lock_io.load_json(STACKS)
    if stacks.get("schema_version") != 1 or set(stacks) != {"schema_version", "stacks"} or not isinstance(stacks["stacks"], dict):
        raise lock_io.LockError(f"{STACKS}: invalid stack choices")
    for stack, selected in stacks["stacks"].items():
        lock_io.require_string_list(selected, f"{stack}.plugins")
        if any(name not in plugins for name in selected):
            raise lock_io.LockError(f"{STACKS}: unknown plugin in stack {stack}")
    return {"plugins": plugins, "profiles": profiles, "organizations": organizations, "stacks": stacks["stacks"]}


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


def refuse_employer_conflict(root: Path, profile: str, roster: dict, environ: Mapping[str, str]) -> None:
    """Refuse a profile other than the one a matching installed employer's organization extends."""
    for employer, evidence in declarations.matching_employers(root, declarations.installed_employers(root, environ)):
        permitted = (roster["organizations"].get(employer.plugin)
                     if employer.marketplace == plugin_selection.MARKETPLACE_NAME else None)
        if profile != permitted:
            reason = (f"select {permitted}, the profile {employer.plugin} extends" if permitted else
                      f"{employer.id} is not an organization in the {plugin_selection.MARKETPLACE_NAME} roster")
            raise lock_io.LockError(f"Cannot select {profile}: the installed employer declaration {employer.id} "
                                    f"matches this repository ({', '.join(evidence)}); {reason}")


def selection_document(root: Path, requested: set[str], foreign: set[str], removals: set[str], declared: dict, roster: dict, environ: Mapping[str, str]) -> tuple[dict, dict[str, dict]]:
    """Return the selection document and the published declarations of its dependency closure.

    The one-profile rule, the employer check and the removal guard evaluate the closure that the published
    catalogs' dependencies define, the same closure settings generation enables.
    """
    selection = plugin_selection.resolve(root)
    kept: list[str] = []
    marketplaces = {}
    if selection.is_file():
        document = lock_io.load_json(selection)
        marketplaces, kept = plugin_selection.validate(document)
        kept = [identity for identity in kept if identity not in removals and (
            plugin_selection.split(identity)[1] != plugin_selection.MARKETPLACE_NAME
            or plugin_selection.split(identity)[0] in roster["plugins"])]
        requested |= {plugin_selection.split(identity)[0] for identity in kept
                      if plugin_selection.split(identity)[1] == plugin_selection.MARKETPLACE_NAME
                      and plugin_selection.split(identity)[0] in roster["plugins"]}
    elif not requested and not foreign and not removals:
        requested = set(plugin_selection.CORE_PLUGINS)
    unknown = sorted(requested - roster["plugins"])
    if unknown:
        raise lock_io.LockError("Unknown plugin: " + ", ".join(unknown))
    selected = {plugin_selection.plugin_id(name) for name in requested} | set(kept) | foreign
    selected -= removals
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
    validated_marketplaces, identities = plugin_selection.validate(document)
    published = published_harness.declarations(validated_marketplaces, identities)
    closure = set(published) | set(identities)
    profiles = sorted(plugin_selection.split(identity)[0] for identity in closure
                      if plugin_selection.split(identity)[1] == plugin_selection.MARKETPLACE_NAME
                      and plugin_selection.split(identity)[0] in roster["profiles"])
    if len(profiles) > 1:
        raise lock_io.LockError("A repository selects one profile, not " + " and ".join(profiles)
                                + "; keep one and drop the others with --remove-plugin <name>")
    if profiles:
        refuse_employer_conflict(root, profiles[0], roster, environ)
    blocked = sorted(closure & removals)
    if blocked:
        raise lock_io.LockError("Cannot remove prerequisite required by remaining plugins: " + ", ".join(blocked))
    return document, published


def plan(root: Path, stacks: list[str], plugins: list[str], removals: list[str], scope: str | None, marketplaces: list[str] = (), *, roster: dict | None = None, exclusions: dict[Path, str] | None = None, environ: Mapping[str, str] = NO_ENVIRONMENT) -> tuple[dict[Path, str | None], Path]:
    root = root.expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise lock_io.LockError(f"Repository root is not a directory: {root}")
    roster = load_roster() if roster is None else roster
    names, foreign = requested_plugins(stacks, plugins, roster)
    declared = {}
    for value in marketplaces:
        name, source = parse_marketplace(value)
        if name in declared:
            raise lock_io.LockError(f"--marketplace {name} is given more than once")
        declared[name] = source
    document, published = selection_document(root, names, foreign, removal_identities(removals), declared, roster, environ)
    selection = root / ".agents" / plugin_selection.SELECTION_NAME
    host_targets = repo_config.plan(selection, root=root, scope=scope, prospective_paths=[selection], lock_document=document,
                                    exclusions=exclusions, declared=published)
    targets = {selection: json.dumps(document, indent=2, ensure_ascii=False) + "\n", **host_targets}
    return targets, root


def run(root: Path, stacks: list[str], plugins: list[str], mode: str, scope: str | None = None, marketplaces: list[str] = (), removals: list[str] = (), *, roster: dict | None = None, environ: Mapping[str, str] = NO_ENVIRONMENT) -> list[str]:
    targets, root = plan(root, stacks, plugins, removals, scope, marketplaces, roster=roster, environ=environ)
    changed = {path: value for path, value in targets.items() if repo_config.text_at(path) != value}
    drift = [path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path) for path in changed]
    if mode == "write":
        repo_config.write_batch(changed)
    return drift


def current_roster() -> dict:
    import update_marketplace_plugins

    with tempfile.TemporaryDirectory(prefix="tj-agents-marketplace-") as directory:
        source = Path(directory) / "marketplace"
        result = process.run(
            ["git", "clone", "--quiet", "--depth=1", "--branch", "main",
             "https://github.com/tj-agents/marketplace.git", str(source)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        )
        if result.returncode:
            raise lock_io.LockError("Cannot reconcile plugins against Marketplace main: "
                                    + (result.stderr or result.stdout).strip())
        return load_roster(json.loads(update_marketplace_plugins.snapshot(source)))


def reconcile(root: Path, mode: str, environ: Mapping[str, str] = NO_ENVIRONMENT) -> list[str]:
    root = root.expanduser().resolve()
    selection = plugin_selection.resolve(root)
    if not selection.is_file():
        return []
    plugin_selection.validate(lock_io.load_json(selection))
    return run(root, [], [], mode, roster=current_roster(), environ=environ)


def reconcile_many(roots: list[Path], mode: str, environ: Mapping[str, str] = NO_ENVIRONMENT) -> list[str]:
    roots = sorted({root.expanduser().resolve() for root in roots
                    if plugin_selection.resolve(root).is_file()})
    if not roots:
        return []
    roster = current_roster()
    changes = {}
    exclusions = {}
    drift = []
    for root in roots:
        targets, _ = plan(root, [], [], [], None, roster=roster, exclusions=exclusions, environ=environ)
        for path, value in targets.items():
            if path in exclusions:
                continue
            if repo_config.text_at(path) != value:
                if path in changes and changes[path] != value:
                    raise lock_io.LockError(f"Conflicting repository configuration targets: {path}")
                changes[path] = value
                drift.append(f"{root}: {path.relative_to(root).as_posix()}")
    for path, value in exclusions.items():
        if repo_config.text_at(path) != value:
            changes[path] = value
            drift.append(str(path))
    if mode == "write":
        repo_config.write_batch(changes)
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
    parser.add_argument("--converge-existing", action="store_true")
    parser.add_argument("--also-root", action="append", type=Path, default=[])
    arguments = parser.parse_args(argv)
    try:
        if arguments.converge_existing:
            if arguments.stack or arguments.plugin or arguments.remove_plugin or arguments.marketplace or arguments.scope:
                raise lock_io.LockError("--converge-existing cannot change the requested selection or scope")
            drift = (reconcile_many([arguments.root, *arguments.also_root], arguments.mode, os.environ)
                     if arguments.also_root else reconcile(arguments.root, arguments.mode, os.environ))
        else:
            if arguments.also_root:
                raise lock_io.LockError("--also-root requires --converge-existing")
            drift = run(arguments.root, arguments.stack, arguments.plugin, arguments.mode, arguments.scope, arguments.marketplace,
                        arguments.remove_plugin, environ=os.environ)
    except (lock_io.LockError, OSError, UnicodeError, ValueError, subprocess.SubprocessError) as error:
        print(f"Plugin adoption failed: {error}", file=sys.stderr)
        return 1
    if drift:
        prefix = "Updated" if arguments.mode == "write" else "Would update" if arguments.mode == "preview" else "Drift in"
        print(prefix + ": " + ", ".join(drift))
    return int(arguments.mode == "check" and bool(drift))


if __name__ == "__main__":
    raise SystemExit(main())
