"""Find installed tier declarations and match employer declarations to a repository.

A plugin payload may ship a `tier.json` declaration. An employer declaration (schema 3
with an `employer` object) detects its organization's repositories by their Git
origin only: `remote_host` names the exact host, `remote` is a case-insensitive pattern
searched in `owner/name`, and `all`/`any` combine them. Core's tier gate and Agent's
plugin selection both read installed declarations and origins through this module.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from tj_cli import process

from tj_agent.host import Host

DECLARATION_NAME = "tier.json"
ORPHAN_MARKER = ".orphaned_at"
GIT_TIMEOUT = 10
HOST_NAME = re.compile(
    r"(?=.{1,253}\.?\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?\Z"
)
ORIGIN_SCHEMES = ("https", "http", "ssh", "git")


@dataclass(frozen=True, slots=True)
class Origin:
    """A repository's Git origin: its lowercase host and lowercase `owner/name`."""

    host: str
    repository: str


@dataclass(frozen=True, slots=True)
class Installed:
    """The installed version of one plugin's `tier.json`, named by its cache path."""

    plugin: str
    marketplace: str
    path: Path
    orphaned: bool

    @property
    def id(self) -> str:
        return f"{self.plugin}@{self.marketplace}"


@dataclass(frozen=True, slots=True)
class Employer:
    """An installed employer declaration and the source repositories it owns."""

    plugin: str
    marketplace: str
    detect: Mapping[str, object]
    owners: tuple[str, ...]

    @property
    def id(self) -> str:
        return f"{self.plugin}@{self.marketplace}"


def parse_origin(url: str) -> Origin | None:
    """The origin a network Git URL names; None for a local or malformed URL."""
    if not url or re.search(r"[\s\x00-\x1f\x7f\\]", url):
        return None
    if "://" in url:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return None
        if (
            parts.scheme not in ORIGIN_SCHEMES
            or not parts.hostname
            or "?" in url
            or "#" in url
            or parts.netloc.endswith(":")
            or (port is not None and not 1 <= port <= 65535)
        ):
            return None
        host, path = parts.hostname, parts.path.lstrip("/")
    else:
        match = re.fullmatch(r"[^@/:]+@([^/:]+):(.+)", url)
        if match is None:
            return None
        host, path = match[1], match[2].lstrip("/")
    segments = path.rstrip("/").split("/")
    if (
        not HOST_NAME.match(host)
        or not path
        or any(part in ("", ".", "..") for part in segments)
    ):
        return None
    return Origin(
        host.rstrip(".").lower(), "/".join(segments[-2:]).removesuffix(".git").lower()
    )


def repository_origin(root: Path) -> Origin | None:
    """The origin of the repository at `root`; None when it has none or Git fails."""
    try:
        completed = process.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GIT_TIMEOUT,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return parse_origin(completed.stdout.removesuffix("\n").removesuffix("\r"))


def origin_predicate(node: object) -> bool:
    """Whether `node` is a well-formed predicate over the origin alone."""
    if not isinstance(node, dict) or len(node) != 1:
        return False
    ((operator, value),) = node.items()
    if operator in ("all", "any"):
        return (
            isinstance(value, list)
            and bool(value)
            and all(origin_predicate(child) for child in value)
        )
    if operator == "remote_host":
        return isinstance(value, str) and HOST_NAME.match(value) is not None
    if operator == "remote":
        if not isinstance(value, str) or not value:
            return False
        try:
            re.compile(value)
        except re.error:
            return False
        return True
    return False


def origin_evidence(
    node: Mapping[str, object], origin: Origin | None
) -> tuple[str, ...] | None:
    """The evidence an origin predicate matches `origin` with; None for no match."""
    ((operator, value),) = node.items()
    if operator in ("all", "any"):
        assert isinstance(value, list)
        children = [origin_evidence(child, origin) for child in value]
        matched = [child for child in children if child is not None]
        if not matched or (operator == "all" and len(matched) != len(children)):
            return None
        return tuple(dict.fromkeys(item for child in matched for item in child))
    if origin is None:
        return None
    assert isinstance(value, str)
    if operator == "remote_host":
        return (
            ("origin host " + origin.host,)
            if origin.host == value.rstrip(".").lower()
            else None
        )
    if operator == "remote":
        return (
            ("origin " + origin.repository,)
            if re.search(value, origin.repository, re.IGNORECASE)
            else None
        )
    raise ValueError(f"Not an origin predicate operator: {operator}")


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _resolved_key(path: Path | str) -> str:
    try:
        return _key(Path(path).resolve())
    except (OSError, ValueError):
        return _key(path)


def _within(key: str, ancestor: str) -> bool:
    return key == ancestor or key.startswith(ancestor.rstrip("\\/") + os.sep)


def _cache_of(payload: Path) -> Path | None:
    return (
        payload.parents[2]
        if len(payload.parents) >= 3 and payload.parents[2].name == "cache"
        else None
    )


def cache_roots() -> list[Path]:
    """Host plugin caches in priority order: the running payload's cache first."""
    roots: list[Path] = []

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.resolve()
        except (OSError, ValueError):
            return
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)

    for variable in dict.fromkeys(host.plugin_root_variable for host in Host):
        value = os.environ.get(variable)
        if value:
            with contextlib.suppress(OSError, ValueError):
                add(_cache_of(Path(value).resolve()))
    # Agent's own payload ships this runtime as <payload>/.agents/agent/runtime; a
    # vendoring plugin as <payload>/dependencies/agent/.agents/agent/runtime.
    runtime = Path(__file__).resolve().parents[1]
    for depth in (2, 4):
        if len(runtime.parents) > depth:
            add(_cache_of(runtime.parents[depth]))
    for host in Host:
        value = os.environ.get(host.home_variable)
        if value:
            add(Path(value) / "plugins" / "cache")
    for host in Host:
        add(Path.home() / host.home_directory / "plugins" / "cache")
    return roots


def installed_declarations(
    root: Path, project: Path | None = None
) -> dict[Path, int] | None:
    """Each installed `tier.json` the registry beside this cache names, with its rank.

    None when the cache has no readable registry. A project-scoped install outranks the
    user-scope one, the most specific project first; one scoped to another project does
    not count.
    """
    registry = Path(root).parent / "installed_plugins.json"
    try:
        plugins = json.loads(registry.read_text(encoding="utf-8-sig")).get("plugins")
    except (OSError, UnicodeError, ValueError, AttributeError):
        return None
    if not isinstance(plugins, dict):
        return None
    cache = _resolved_key(root)
    here = _resolved_key(project) if project is not None else None
    ranked: dict[Path, int] = {}
    for entries in plugins.values():
        for entry in entries if isinstance(entries, list) else ():
            location = entry.get("installPath") if isinstance(entry, dict) else None
            if not isinstance(location, str) or not location:
                continue
            scoped = entry.get("projectPath")
            rank = -1
            if isinstance(scoped, str) and scoped:
                scope = _resolved_key(scoped)
                if here is None or not _within(here, scope):
                    continue
                rank = len(scope)
            ancestor = Path(location).parent.parent.parent
            if _key(ancestor) != cache and _resolved_key(ancestor) != cache:
                continue
            path = Path(location) / DECLARATION_NAME
            ranked[path] = max(rank, ranked.get(path, rank))
    return ranked


def installed(roots: Iterable[Path], project: Path | None = None) -> list[Installed]:
    """One declaration per plugin: the first cache root that has it, at its installed
    version.

    A cache with a host registry beside it answers only for its installed versions; one
    without falls back to every cached version. Within a cache the most specific install
    scope wins, then a live payload over an orphaned one, then the newest directory. A
    later cache replaces an orphaned one.
    """
    found: dict[str, Installed] = {}
    for root in roots:
        ranked = installed_declarations(root, project)
        if ranked is None:
            try:
                ranked = dict.fromkeys(
                    sorted(Path(root).glob("*/*/*/" + DECLARATION_NAME)), -1
                )
            except OSError:
                continue
        candidates: dict[str, tuple[tuple[int, bool, float], Installed]] = {}
        for path, rank in sorted(ranked.items()):
            if not path.exists():
                continue
            payload = path.parent
            item = Installed(
                payload.parent.name,
                payload.parent.parent.name,
                path,
                (payload / ORPHAN_MARKER).exists(),
            )
            try:
                modified = payload.stat().st_mtime
            except OSError:
                modified = 0.0
            preference = (rank, not item.orphaned, modified)
            current = candidates.get(item.id)
            if current is None or preference > current[0]:
                candidates[item.id] = (preference, item)
        for identity, (_, item) in candidates.items():
            existing = found.get(identity)
            if existing is None or (existing.orphaned and not item.orphaned):
                found[identity] = item
    return list(found.values())


def installed_employers(project: Path) -> list[Employer]:
    """Live employer declarations installed for `project` that detect by origin."""
    employers = []
    for item in installed(cache_roots(), project):
        if item.orphaned:
            continue
        try:
            data = json.loads(item.path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            continue
        if (
            not isinstance(data, dict)
            or data.get("schema_version") != 3
            or not isinstance(data.get("employer"), dict)
            or not origin_predicate(data.get("detect"))
        ):
            continue
        owner = data.get("owner_repository")
        owners = (
            [owner]
            if isinstance(owner, str)
            else owner
            if isinstance(owner, list)
            else []
        )
        employers.append(
            Employer(
                item.plugin,
                item.marketplace,
                data["detect"],
                tuple(name.lower() for name in owners if isinstance(name, str)),
            )
        )
    return employers


def matching_employers(
    root: Path, employers: Iterable[Employer]
) -> list[tuple[Employer, tuple[str, ...]]]:
    """Each employer whose origin predicate or own source repository matches `root`."""
    candidates = list(employers)
    origin = repository_origin(root) if candidates else None
    if origin is None:
        return []
    matches = []
    for employer in candidates:
        evidence = origin_evidence(employer.detect, origin)
        if evidence is None and origin.repository in employer.owners:
            evidence = ("source repository " + origin.repository,)
        if evidence is not None:
            matches.append((employer, evidence))
    return matches
