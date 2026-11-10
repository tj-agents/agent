"""Plugins Codex's user config enables, which `codex plugin add` always extends."""

from __future__ import annotations

import tomllib
from collections.abc import Collection
from pathlib import Path

from tj_agent.host import Host


def path() -> Path:
    return Path(Host.CODEX.configuration_root) / "config.toml"


def enabled(text: str) -> set[str]:
    plugins = tomllib.loads(text).get("plugins")
    if not isinstance(plugins, dict):
        return set()
    return {
        identity
        for identity, settings in plugins.items()
        if isinstance(settings, dict) and settings.get("enabled") is True
    }


def without(text: str, identities: Collection[str]) -> str:
    """`text` without the user-level `[plugins."<identity>"]` tables of `identities`."""
    before = tomllib.loads(text)
    plugins = before.get("plugins")
    if not isinstance(plugins, dict) or not set(identities) & set(plugins):
        return text
    remaining = {k: v for k, v in plugins.items() if k not in identities}
    expected = {k: v for k, v in before.items() if k != "plugins"}
    if remaining:
        expected["plugins"] = remaining
    kept = []
    statement = ""
    dropping = False
    for line in text.splitlines(keepends=True):
        statement += line
        try:
            value = tomllib.loads(statement)
        except tomllib.TOMLDecodeError:
            continue
        if statement.lstrip().startswith("["):
            dropping = table_identity(value) in identities
        if not dropping:
            kept.append(statement)
        statement = ""
    if statement:
        raise ValueError("Cannot identify complete Codex settings statements")
    result = "".join(kept)
    if tomllib.loads(result) != expected:
        raise ValueError("Removing user plugin entries would change other settings")
    return result


def table_identity(value: dict[str, object]) -> str | None:
    plugins = value.get("plugins")
    if len(value) != 1 or not isinstance(plugins, dict) or len(plugins) != 1:
        return None
    identity, settings = next(iter(plugins.items()))
    return identity if settings == {} else None
