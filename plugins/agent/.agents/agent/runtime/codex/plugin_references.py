"""Codex configuration and known project roots for a plugin retirement audit."""

from pathlib import Path

from codex import projects as configured
from tj_agent.host import Host


def files() -> tuple[Path, ...]:
    return (Host.CODEX.configuration_root / "config.toml",)


def projects() -> set[Path]:
    value = configured.user_config().get("projects", {})
    return {Path(path) for path in value} if isinstance(value, dict) else set()
