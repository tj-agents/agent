"""Codex's installed plugin build labels (Python 3.9+)."""

from __future__ import annotations

from host import Host

HOST = Host.CODEX


def label(marketplace: str, plugin: str) -> str | None:
    directory = HOST.configuration_root / "plugins" / "cache" / marketplace / plugin
    try:
        builds = [entry.name for entry in directory.iterdir() if entry.is_dir()]
    except OSError:
        return None
    return builds[0] if len(builds) == 1 else None
