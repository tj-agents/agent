"""The contract each native host's launch adapter implements."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class Confirmation(StrEnum):
    DISPATCHED = "dispatched"
    PROCESS_STARTED = "process-started"
    FIRST_TURN = "first-turn"


@dataclass(frozen=True)
class Liveness:
    alive: bool
    activity: str | None = None
    background: str | None = None
    process_ids: tuple[int, ...] | None = None
    launch_ids: tuple[str, ...] | None = None
    processes_reloadable: bool = False


@dataclass(frozen=True, slots=True)
class PreparedLaunch:
    command: tuple[str, ...]
    session: str | None = None
    created: bool = False


@dataclass(frozen=True, slots=True)
class TerminalEnvironment:
    """An unset Windows terminal type lets the provider detect its console."""

    windows_term: str | None
    force_colour: bool

    def cleared(self, *, windows: bool) -> tuple[str, ...]:
        if windows and self.windows_term is None:
            return ("NO_COLOR", "TERM")
        return ("NO_COLOR",)

    def forced(self, *, windows: bool) -> dict[str, str]:
        settings = {"FORCE_COLOR": "1"} if self.force_colour else {}
        if windows and self.windows_term is not None:
            settings["TERM"] = self.windows_term
        return settings


class Launch(Protocol):
    SESSION_ENVIRONMENT: tuple[str, ...]
    TERMINAL_ENVIRONMENT: TerminalEnvironment

    def executable(self) -> str: ...

    def managed_arguments(self, arguments: Sequence[str]) -> tuple[str, ...]: ...

    def prepare_foreground(self, executable: str, directory: Path,
                           arguments: Sequence[str], cli: str,
                           environ: Mapping[str, str]) -> PreparedLaunch: ...

    def prepare(self, directory: Path, title: str, arguments: list[str], cli: str, environ: Mapping[str, str]) -> PreparedLaunch: ...

    def abandon(self, prepared: PreparedLaunch, launch_id: str, dispatched: bool) -> None: ...

    def confirm(self, prepared: PreparedLaunch, arguments: list[str], launch_id: str) -> Confirmation: ...

    def start_arguments(self, directory: Path, title: str, prompt: str, model: str | None,
                        effort: str | None) -> list[str]: ...

    def resume_arguments(self, directory: Path, title: str, session_id: str) -> list[str]: ...

    def liveness(self, *, strict: bool = False) -> dict[str, Liveness]: ...

    def respawn(self, background: str) -> None: ...

    def name_session(self, session_id: str, title: str) -> None: ...

    def record_session_environment(
        self, session_id: str, environ: Mapping[str, str], *, cleared: tuple[str, ...] = ()
    ) -> None: ...
