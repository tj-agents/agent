"""Claude's installed-plugin registry: entries whose project no longer exists, and build updates."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile

PROJECT_SCOPES = frozenset({"project", "local"})


def _read(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), dict):
        raise ValueError(f"{path} has no plugins object")
    return data


def _gone(entry: object) -> bool:
    if not isinstance(entry, dict) or entry.get("scope") not in PROJECT_SCOPES:
        return False
    project = entry.get("projectPath")
    if not isinstance(project, str) or not project.strip():
        return False
    path = Path(project)
    return path.is_absolute() and Path(path.anchor).exists() and not path.exists()


def orphaned(path: Path) -> list[tuple[str, str]]:
    return [(identity, entry["projectPath"]) for identity, entries in _read(path)["plugins"].items()
            if isinstance(entries, list) for entry in entries if _gone(entry)]


def remove_orphaned(path: Path) -> list[tuple[str, str]]:
    data = _read(path)
    removed = []
    for identity in list(data["plugins"]):
        entries = data["plugins"][identity]
        if not isinstance(entries, list):
            continue
        kept = [entry for entry in entries if not _gone(entry)]
        removed += [(identity, entry["projectPath"]) for entry in entries if _gone(entry)]
        if kept:
            data["plugins"][identity] = kept
        else:
            del data["plugins"][identity]
    if removed:
        _write(path, data)
    return removed


@dataclass(frozen=True, slots=True)
class Build:
    """A cached plugin build that the plugin's rows in `scopes` should load."""

    root: Path
    revision: str
    scopes: tuple[str, ...]


def record_builds(path: Path, builds: Mapping[str, Build], applies: Callable[[str, object], bool],
                  updated: str) -> None:
    """Point each plugin's applicable rows at its new build, as `claude plugin update` records it."""
    data = _read(path)
    for identity, build in builds.items():
        entries = data["plugins"].get(identity)
        for entry in entries if isinstance(entries, list) else []:
            if (isinstance(entry, dict) and entry.get("scope") in build.scopes
                    and applies(entry["scope"], entry.get("projectPath"))):
                entry["installPath"] = str(build.root)
                if "version" in entry:
                    entry["version"] = build.root.name
                entry.pop("resolvedVersion", None)
                entry["lastUpdated"] = updated
                entry["gitCommitSha"] = build.revision
    _write(path, data)


def _write(path: Path, data: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
