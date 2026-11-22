from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

import jobs
from tj_agent.host import Host
from tree_digest import Digests

RECEIPT = "codex-plugin-versions.json"


@dataclass(frozen=True, slots=True)
class Build:
    source: str
    version: str
    digest: str | None


def source_identity(source: Mapping[str, object]) -> str:
    normalized = dict(source)
    path = normalized.get("path")
    if isinstance(path, str):
        normalized["path"] = PurePosixPath(path).as_posix()
    return json.dumps(normalized, sort_keys=True)


def recorded(state: Path) -> dict[str, Build]:
    try:
        data = json.loads((state / RECEIPT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    found = {}
    for identity, entry in data.items():
        if not isinstance(entry, dict):
            continue
        source, version, digest = (
            entry.get("source"),
            entry.get("version"),
            entry.get("digest"),
        )
        if (
            isinstance(source, str)
            and isinstance(version, str)
            and (digest is None or isinstance(digest, str))
        ):
            found[identity] = Build(source, version, digest)
    return found


def root(marketplace: str, plugin: str, version: str) -> Path | None:
    if any(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) is None
        for part in (marketplace, plugin, version)
    ):
        return None
    return (
        Path(Host.CODEX.configuration_root)
        / "plugins/cache"
        / marketplace
        / plugin
        / version
    )


def label(path: Path) -> str | None:
    try:
        data = json.loads(
            (path / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else None


def remember(entries: Iterable[object], *, verified_install: bool = False) -> None:
    state = jobs.state_directory()
    builds = recorded(state)
    before = dict(builds)
    digests = Digests(state)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identity, version, source = (
            entry.get("pluginId"),
            entry.get("version"),
            entry.get("source"),
        )
        if entry.get("installed") is False or entry.get("error") or entry.get("errors"):
            if isinstance(identity, str):
                builds.pop(identity, None)
            continue
        if (
            not isinstance(identity, str)
            or not isinstance(version, str)
            or not isinstance(source, dict)
        ):
            continue
        if not verified_install:
            continue
        plugin, _, marketplace = identity.rpartition("@")
        try:
            expected = source_identity(source)
        except (TypeError, ValueError):
            continue
        installed = root(marketplace, plugin, version)
        if installed is None or label(installed) != version:
            continue
        immutable = bool(re.fullmatch(r"[0-9a-f]{12}", version))
        digest = None if immutable else digests.tree(installed)
        builds[identity] = Build(expected, version, digest)
    digests.save()
    if builds != before:
        state.mkdir(parents=True, exist_ok=True)
        (state / RECEIPT).write_text(
            json.dumps({identity: asdict(build) for identity, build in builds.items()}),
            encoding="utf-8",
        )


def current(marketplace: str, plugin: str, digests: Digests) -> bool:
    build = recorded(digests.path.parent).get(f"{plugin}@{marketplace}")
    if build is None:
        return False
    installed = root(marketplace, plugin, build.version)
    if installed is None or label(installed) != build.version:
        return False
    if re.fullmatch(r"[0-9a-f]{12}", build.version):
        return True
    return build.digest is not None and digests.tree(installed) == build.digest
