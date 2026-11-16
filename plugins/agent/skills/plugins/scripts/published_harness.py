#!/usr/bin/env python3
"""Read the harness permissions marketplaces publish for a repository's selected plugins.

Each selected `<plugin>@<marketplace>` is looked up in its marketplace's Claude catalog on the default
branch. The plugin's `harness.json` is then read at the exact source revision that catalog pins, through a
blobless, depth-one Git fetch using the caller's normal Git configuration and credentials.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import harness_permissions
import lock_io
import plugin_selection
from plugin_runtime import process

CATALOG = ".claude-plugin/marketplace.json"
HARNESS = "harness.json"
DEFAULT_BRANCH = "HEAD"
TIMEOUT_SECONDS = 60


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


def _location(
    identity: str, catalog: Mapping[str, Any], marketplace: Publication
) -> Location:
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
    source = matches[0].get("source")
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
    """Return each selected plugin's validated published permissions by identity."""
    selected = sorted(set(identities))
    names = sorted({plugin_selection.split(identity)[1] for identity in selected})
    with ThreadPoolExecutor() as pool:
        catalogs = dict(
            zip(
                names,
                pool.map(_catalog, [marketplaces[name]["repository"] for name in names]),
                strict=True,
            )
        )
    locations: dict[str, Location] = {}
    for identity in selected:
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
