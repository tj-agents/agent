"""Dispatch native permission configuration to its owning host (Python 3.11+)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from claude import permissions as claude_permissions
from codex import permissions as codex_permissions
from host import Host


class PermissionAdapter(Protocol):
    def settings(self, text: str, mode: str, *, project: bool = False) -> str: ...

    def plan(self, path: Path, mode: str, /, *, applying: bool = False) -> dict[Path, str]: ...


ADAPTERS: Mapping[Host, PermissionAdapter] = {
    Host.CLAUDE: claude_permissions,
    Host.CODEX: codex_permissions,
}


def plan(paths: dict[Host, Path], mode: str, *, applying: bool = False) -> dict[Path, str]:
    targets = {}
    for host, path in paths.items():
        targets.update(ADAPTERS[host].plan(path, mode, applying=applying))
    return targets
