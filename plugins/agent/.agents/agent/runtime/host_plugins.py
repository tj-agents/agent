"""The per-host native plugin contract and its dispatch table (Python 3.9+)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from claude import plugins as claude_plugins
from codex import plugins as codex_plugins
from host import Host


class Plugins(Protocol):
    HOST: Host
    CATALOG: str
    VALUE_OPTIONS: frozenset[str]
    CONFIGURATION_OPTIONS: frozenset[str]
    FLAG_OPTIONS: frozenset[str]
    DIRECTORY_OPTIONS: frozenset[str]
    INFORMATION_OPTIONS: frozenset[str]
    MAINTENANCE_COMMANDS: frozenset[str]
    NODE_SHIM_BYPASSED: bool

    @property
    def reconcile(self) -> Callable[[Path], list[str]] | None: ...

    def resolve_executable(self) -> str: ...

    def selection(self, executable: str, project: Path) -> tuple[dict[str, str | None], list[str]]: ...

    def installations(self, project: Path, entries: list) -> list: ...

    def location(self, entry: dict, project: Path) -> tuple[str, Path] | None: ...

    def require_inventory_options(self, options: tuple[str, ...]) -> None: ...

    def inventory_marketplaces(self, required: tuple[str, ...]) -> Sequence[str | None]: ...

    def inventory_entries(self, data: object) -> object: ...

    def entry_errors(self, entries: list, project: Path) -> list[str]: ...

    def marketplace_location(self, executable: str, project: Path, name: str) -> str | None: ...

    def install_path(self, entry: dict, marketplace: str, plugin: str) -> Path: ...

    def refresh_marketplaces(self, executable: str, project: Path, marketplaces: dict) -> None: ...

    def refresh_plugins(self, executable: str, project: Path, marketplaces: dict, plugins: list[str]) -> list[str]: ...

    def install_missing(self, executable: str, project: Path, missing) -> None: ...

    def update(self, repositories: set[str] | None, project: Path, updated: set[str] | None = None) -> bool: ...

    def ensure_hooks(self, project: Path, executable: str | None = None, *,
                     running_session: bool = False) -> str | None: ...

    def adopt(self, project: Path, updated: set[str], reason: str | None, allowed: bool) -> dict | None: ...


PLUGINS: Mapping[Host, Plugins] = {Host.CLAUDE: claude_plugins, Host.CODEX: codex_plugins}
