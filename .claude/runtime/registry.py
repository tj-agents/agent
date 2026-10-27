"""Claude's installed-plugin registry entries whose project no longer exists (Python 3.9+)."""

from __future__ import annotations

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
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return removed
