#!/usr/bin/env python3
"""Read the harness permissions marketplaces publish for a repository's selected plugins.

Each selected `<plugin>@<marketplace>` is looked up in its marketplace's Claude catalog on the default
branch, together with every plugin its catalog entry depends on, transitively. Each plugin's `harness.json`
is then read at the exact source revision that catalog pins, through a blobless, depth-one Git fetch using
the caller's normal Git configuration and credentials. Offline, the same dependencies are resolved from the
marketplace catalogs the native hosts last fetched onto this machine.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import harness_permissions
import lock_io
import plugin_selection
from plugin_runtime import Host, process

CATALOG = ".claude-plugin/marketplace.json"
HARNESS = "harness.json"
DEFAULT_BRANCH = "HEAD"
TIMEOUT_SECONDS = 60
LOCAL_CATALOGS = ((Host.CLAUDE, "plugins/marketplaces"), (Host.CODEX, ".tmp/marketplaces"))


@dataclass(frozen=True, slots=True)
class Publication:
    """A revision of a published Git repository."""

    url: str
    revision: str


@dataclass(frozen=True, slots=True)
class Location:
    """One plugin's published harness declaration."""

    publication: Publication
    path: str


def _git(directory: Path, *arguments: str) -> bytes:
    try:
        result = process.run(
            ["git", "-C", str(directory), *arguments],
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise lock_io.LockError(f"git {arguments[0]} could not run: {error}") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise lock_io.LockError(f"git {' '.join(arguments)} failed: {detail}")
    return bytes(result.stdout)


def read(publication: Publication, paths: Iterable[str]) -> tuple[str, dict[str, bytes]]:
    """Return the fetched commit and the named files, fetching no other file contents."""
    with tempfile.TemporaryDirectory(prefix="tj-agents-harness-") as directory:
        root = Path(directory)
        _git(root, "init", "--quiet", "--bare")
        _git(root, "remote", "add", "origin", publication.url)
        _git(
            root,
            "fetch",
            "--quiet",
            "--depth=1",
            "--filter=blob:none",
            "origin",
            publication.revision,
        )
        commit = _git(root, "rev-parse", "FETCH_HEAD").decode("ascii").strip()
        files = {
            path: _git(root, "cat-file", "blob", f"FETCH_HEAD:{path}")
            for path in paths
        }
    return commit, files


def _document(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as error:
        raise lock_io.LockError(f"{label} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise lock_io.LockError(f"{label} must be a JSON object")
    return value


def _harness_path(source: object, label: str) -> str:
    parts = (
        [part for part in PurePosixPath(source).parts if part != "."]
        if isinstance(source, str)
        else []
    )
    if not parts or parts[0] == "/" or ".." in parts:
        raise lock_io.LockError(f"{label}: invalid published plugin path {source!r}")
    return "/".join([*parts, HARNESS])


def _entry(identity: str, catalog: Mapping[str, Any]) -> Mapping[str, Any]:
    name, _ = plugin_selection.split(identity)
    entries = catalog.get("plugins")
    if not isinstance(entries, list):
        raise lock_io.LockError(f"{identity}: its marketplace catalog lists no plugins")
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("name") == name
    ]
    if len(matches) != 1:
        raise lock_io.LockError(f"{identity} is not published by its marketplace")
    return matches[0]


def _location(
    identity: str, catalog: Mapping[str, Any], marketplace: Publication
) -> Location:
    source = _entry(identity, catalog).get("source")
    if isinstance(source, str):
        return Location(marketplace, _harness_path(source, identity))
    if isinstance(source, dict) and source.get("source") == "git-subdir":
        url = source.get("url")
        revision = source.get("sha") or source.get("ref")
        if (
            isinstance(url, str)
            and url.startswith("https://")
            and isinstance(revision, str)
            and revision
            and not revision.startswith("-")
        ):
            return Location(
                Publication(url, revision), _harness_path(source.get("path"), identity)
            )
    raise lock_io.LockError(f"{identity}: unsupported published source {source!r}")


def _dependencies(identity: str, entry: Mapping[str, Any]) -> list[str]:
    declared = entry.get("dependencies", [])
    if not isinstance(declared, list):
        raise lock_io.LockError(f"{identity}: published dependencies must be a list")
    marketplace = plugin_selection.split(identity)[1]
    found = []
    for value in declared:
        name = value.get("name") if isinstance(value, dict) else value
        dependency = (
            name if isinstance(name, str) and "@" in name else f"{name}@{marketplace}"
        )
        if not isinstance(name, str) or not plugin_selection.valid_identity(dependency):
            raise lock_io.LockError(
                f"{identity}: invalid published dependency {value!r}"
            )
        found.append(dependency)
    return found


def closure(
    identities: Iterable[str], catalog: Callable[[str], Mapping[str, Any] | None]
) -> list[str]:
    """Return the identities and every plugin their catalog entries depend on, transitively.

    `catalog` returns a declared marketplace's catalog, or None for a marketplace the selection does not
    declare, which no dependency may name.
    """
    resolved: set[str] = set()
    pending: list[tuple[str, str | None]] = [
        (identity, None) for identity in sorted(set(identities), reverse=True)
    ]
    while pending:
        identity, dependent = pending.pop()
        if identity in resolved:
            continue
        marketplace = plugin_selection.split(identity)[1]
        document = catalog(marketplace)
        if document is None:
            raise lock_io.LockError(
                f"{dependent} depends on {identity}, but the selection does not declare "
                f"the {marketplace} marketplace"
            )
        try:
            entry = _entry(identity, document)
        except lock_io.LockError as error:
            if dependent is None:
                raise
            raise lock_io.LockError(f"{dependent} depends on {identity}: {error}") from error
        resolved.add(identity)
        pending.extend(
            (dependency, identity)
            for dependency in reversed(_dependencies(identity, entry))
            if dependency not in resolved
        )
    return sorted(resolved)


def local_catalog(marketplace: str) -> dict[str, Any]:
    """Return the marketplace catalog Claude, or else Codex, last fetched onto this machine."""
    for host, directory in LOCAL_CATALOGS:
        path = host.configuration_root / directory / marketplace / CATALOG
        if path.is_file():
            return _document(path.read_bytes(), str(path))
    raise lock_io.LockError(
        f"No local {marketplace} marketplace catalog resolves the selected plugins' "
        "dependencies offline; run tj install"
    )


def local_closure(
    marketplaces: Mapping[str, Mapping[str, str]], identities: Iterable[str]
) -> list[str]:
    """Return `closure` resolved from this machine's fetched catalogs, without a network lookup."""
    catalogs: dict[str, dict[str, Any]] = {}

    def catalog(name: str) -> dict[str, Any] | None:
        if name not in marketplaces:
            return None
        if name not in catalogs:
            catalogs[name] = local_catalog(name)
        return catalogs[name]

    return closure(identities, catalog)


def _catalog(repository: str) -> tuple[Publication, dict[str, Any]]:
    commit, files = read(
        Publication(f"https://github.com/{repository}.git", DEFAULT_BRANCH), [CATALOG]
    )
    publication = Publication(f"https://github.com/{repository}.git", commit)
    return publication, _document(files[CATALOG], f"{repository} {CATALOG}")


def _contents(item: tuple[Publication, list[str]]) -> dict[str, bytes]:
    publication, paths = item
    return read(publication, paths)[1]


def declarations(
    marketplaces: Mapping[str, Mapping[str, str]], identities: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Return the validated published permissions of the selected plugins and their dependencies.

    The result is keyed by every identity in the selection's transitive dependency closure.
    """
    names = sorted(marketplaces)
    with ThreadPoolExecutor() as pool:
        catalogs = dict(
            zip(
                names,
                pool.map(_catalog, [marketplaces[name]["repository"] for name in names]),
                strict=True,
            )
        )
    resolved = closure(
        identities, lambda name: catalogs[name][1] if name in catalogs else None
    )
    locations: dict[str, Location] = {}
    for identity in resolved:
        marketplace, catalog = catalogs[plugin_selection.split(identity)[1]]
        locations[identity] = _location(identity, catalog, marketplace)
    wanted: dict[Publication, list[str]] = {}
    for location in locations.values():
        wanted.setdefault(location.publication, []).append(location.path)
    with ThreadPoolExecutor() as pool:
        fetched = dict(zip(wanted, pool.map(_contents, wanted.items()), strict=True))
    result: dict[str, dict[str, Any]] = {}
    for identity, location in locations.items():
        label = f"{identity} published harness"
        document = _document(fetched[location.publication][location.path], label)
        name, marketplace_name = plugin_selection.split(identity)
        if document.get("plugin") != f"{marketplace_name}/{name}":
            raise lock_io.LockError(
                f"{label} declares plugin {document.get('plugin')!r}"
            )
        requires = document.get("requires")
        harness_permissions.validate_requires(requires, label)
        assert isinstance(requires, dict)
        result[identity] = dict(
            requires.get("permissions", harness_permissions.EMPTY_PERMISSIONS)
        )
    return result
