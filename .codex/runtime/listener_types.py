from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class Lifecycle(StrEnum):
    STARTING = 'starting'
    RUNNING = 'running'
    DELIVERING = 'delivering'
    ACCEPTED = 'accepted'
    DELIVERY_UNKNOWN = 'delivery-unknown'
    UNAVAILABLE = 'unavailable'


class Outcome(StrEnum):
    SUCCESS = 'success'
    FAILED = 'failed'
    TIMEOUT = 'timeout'


@dataclass(frozen=True, slots=True)
class BackendIdentity:
    pid: int
    process_start_time: str

    def __post_init__(self) -> None:
        if (type(self.pid) is not int or self.pid <= 0
                or not isinstance(self.process_start_time, str) or not self.process_start_time):
            raise RuntimeError('The listener backend identity is invalid')

    @classmethod
    def from_value(cls, value: Any) -> BackendIdentity:
        if not isinstance(value, dict) or set(value) != {'pid', 'process_start_time'}:
            raise RuntimeError('The listener backend identity is invalid')
        return cls(**value)

    def value(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int | None
    timed_out: bool = False

    @property
    def outcome(self) -> Outcome:
        if self.timed_out:
            return Outcome.TIMEOUT
        return Outcome.SUCCESS if self.returncode == 0 else Outcome.FAILED

    def value(self) -> dict[str, Any]:
        return {'outcome': self.outcome.value, 'returncode': self.returncode}
