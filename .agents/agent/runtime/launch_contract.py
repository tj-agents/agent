"""The contract each native host's launch adapter implements."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Liveness:
    alive: bool
    activity: str | None = None
    background: str | None = None


class Launch(Protocol):
    SESSION_ENVIRONMENT: tuple[str, ...]
    TAB_CLEARED: tuple[str, ...]
    TAB_FORCED: Mapping[str, str]

    def executable(self) -> str: ...

    def start_arguments(self, directory: Path, title: str, prompt: str, model: str | None,
                        effort: str | None) -> list[str]: ...

    def resume_arguments(self, directory: Path, title: str, session_id: str) -> list[str]: ...

    def liveness(self, *, strict: bool = False) -> dict[str, Liveness]: ...

    def respawn(self, background: str) -> None: ...

    def name_session(self, session_id: str, title: str) -> None: ...

    def record_session_environment(self, session_id: str, environ: Mapping[str, str]) -> None: ...
