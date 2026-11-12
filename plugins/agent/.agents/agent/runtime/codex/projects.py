"""Projects Codex trusts that carry their own Codex configuration."""

from __future__ import annotations

import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path

from tj_agent.host import Host


def user_config() -> dict[str, object]:
    path = Host.CODEX.configuration_root / "config.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def trusted(config: Mapping[str, object], temporary: Path | None = None) -> list[Path]:
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        return []
    temporary = (temporary or Path(tempfile.gettempdir())).resolve()
    return sorted(
        {
            Path(path).resolve()
            for path, settings in projects.items()
            if isinstance(settings, dict)
            and settings.get("trust_level") == "trusted"
            and (Path(path) / ".codex/config.toml").is_file()
            and not Path(path).resolve().is_relative_to(temporary)
        }
    )
