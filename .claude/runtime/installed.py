"""Claude's installed plugin builds against its marketplace pins (Python 3.9+)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

from host import Host

HOST = Host.CLAUDE
RELOAD = "type /reload-plugins"
COMMAND_PREFIX = "! "
BUILD = re.compile(r"([0-9a-f]{12})-[0-9a-f]+")


def read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def pins(marketplace: str) -> dict[str, str]:
    catalog = read(HOST.configuration_root / "plugins" / "marketplaces" / marketplace / ".claude-plugin"
                   / "marketplace.json")
    entries = catalog.get("plugins", []) if isinstance(catalog, dict) else []
    return {entry["name"]: entry["source"]["sha"] for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("source"), dict)
            and isinstance(entry["source"].get("sha"), str)}


def installs(marketplace: str) -> dict[str, tuple[str, Path]]:
    registry = read(HOST.configuration_root / "plugins" / "installed_plugins.json")
    plugins = registry.get("plugins", {}) if isinstance(registry, dict) else {}
    found = {}
    for plugin_id, rows in plugins.items():
        name, _, source = plugin_id.rpartition("@")
        if source != marketplace or not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("scope") == "user" and isinstance(row.get("gitCommitSha"), str):
                found[name] = (row["gitCommitSha"], Path(row.get("installPath", "")))
    return found


def running_root() -> Path | None:
    value = os.environ.get("CLAUDE_PLUGIN_ROOT")
    return Path(value) if value else None


def installed_build_behind(marketplace: str, plugin: str, install: Path) -> str | None:
    return None


def loaded_build_behind(root: Path, pin: str, install: Path) -> str | None:
    build = BUILD.fullmatch(root.name)
    if build and build.group(1) != pin[:12]:
        return f"this session loaded {build.group(1)}, current {pin[:12]}"
    return None
