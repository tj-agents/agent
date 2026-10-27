"""The native agent hosts this runtime supports (Python 3.9+)."""

from __future__ import annotations

from enum import Enum
import os
from pathlib import Path


class Host(Enum):
    CLAUDE = ("claude", "CLAUDE_CONFIG_DIR", ".claude", "CLAUDE_CODE_SESSION_ID", "projects")
    CODEX = ("codex", "CODEX_HOME", ".codex", "CODEX_THREAD_ID", "sessions")

    def __new__(cls, value: str, *_data: str) -> Host:
        member = object.__new__(cls)
        member._value_ = value
        return member

    def __init__(self, value: str, home_variable: str, home_directory: str, session_variable: str,
                 history_directory: str) -> None:
        self.home_variable = home_variable
        self.home_directory = home_directory
        self.session_variable = session_variable
        self.history_directory = history_directory

    def __str__(self) -> str:
        return self.value

    @property
    def configuration_root(self) -> Path:
        return Path(os.environ.get(self.home_variable) or Path.home() / self.home_directory)

    @property
    def history_root(self) -> Path:
        return self.configuration_root / self.history_directory

    @property
    def manifest_directory(self) -> str:
        return f".{self.value}-plugin"
