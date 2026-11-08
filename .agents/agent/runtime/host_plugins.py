"""The per-host native plugin contract and its dispatch table (Python 3.9+)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from claude import plugins as claude_plugins
from codex import plugins as codex_plugins
from tj_agent.host import Host


class Plugins(Protocol):
    HOST: Host
    VALUE_OPTIONS: frozenset[str]
    CONFIGURATION_OPTIONS: frozenset[str]
    FLAG_OPTIONS: frozenset[str]
    DIRECTORY_OPTIONS: frozenset[str]
    INFORMATION_OPTIONS: frozenset[str]
    MAINTENANCE_COMMANDS: frozenset[str]
    NODE_SHIM_BYPASSED: bool

    def resolve_executable(self) -> str: ...

    def installations(self, project: Path, entries: list) -> list: ...

    def location(self, entry: dict, project: Path) -> tuple[str, Path] | None: ...

    def require_inventory_options(self, options: tuple[str, ...]) -> None: ...

    def inventory_marketplaces(self, required: tuple[str, ...]) -> Sequence[str | None]: ...

    def inventory_entries(self, data: object) -> object: ...

    def ensure_hooks(self, project: Path, executable: str | None = None, *,
                     running_session: bool = False,
                     required_roots: Mapping[str, Path] | None = None) -> str | None: ...


PLUGINS: Mapping[Host, Plugins] = {Host.CLAUDE: claude_plugins, Host.CODEX: codex_plugins}
