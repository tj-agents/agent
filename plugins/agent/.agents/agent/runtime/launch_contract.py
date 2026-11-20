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


class Launch(Protocol):
    SESSION_ENVIRONMENT: tuple[str, ...]
    TAB_CLEARED: tuple[str, ...]
    TAB_FORCED: Mapping[str, str]

    def executable(self) -> str: ...

    def managed_arguments(self, arguments: Sequence[str]) -> tuple[str, ...]: ...

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
