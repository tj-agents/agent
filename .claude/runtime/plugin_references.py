"""Claude configuration and registered project roots for a plugin retirement audit."""

import json
from pathlib import Path

from tj_agent.host import Host


def files() -> tuple[Path, ...]:
    root = Host.CLAUDE.configuration_root
    return (
        root / "settings.json",
        root / "settings.local.json",
        root / "plugins/installed_plugins.json",
        root / "plugins/known_marketplaces.json",
    )


def projects() -> set[Path]:
    registry = Host.CLAUDE.configuration_root / "plugins/installed_plugins.json"
    if not registry.is_file():
        return set()
    value = json.loads(registry.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or not isinstance(value.get("plugins"), dict):
        raise ValueError(f"Invalid native plugin registry: {registry}")
    projects: set[Path] = set()
    for rows in value.get("plugins", {}).values():
        if isinstance(rows, list):
            projects.update(
                Path(row["projectPath"])
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("projectPath"), str)
            )
    return projects
