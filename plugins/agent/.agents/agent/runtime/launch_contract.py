"""The session environment contract shared by native launch adapters."""

from typing import Protocol


class Launch(Protocol):
    SESSION_ENVIRONMENT: tuple[str, ...]
    RELAUNCH_ENVIRONMENT: tuple[str, ...]
