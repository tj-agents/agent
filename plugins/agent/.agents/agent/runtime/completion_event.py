"""Host-neutral events accepted by the managed completion protocol."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class EventError(ValueError):
    pass


class Kind(StrEnum):
    START = "start"
    END = "end"
    INTERRUPT = "interrupt"


def text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise EventError(f"Native completion event has no {field}")
    return value


@dataclass(frozen=True, slots=True)
class Event:
    session: str
    turn: str
    kind: Kind
    report: str | None = None
    pending: str | None = None

    def __post_init__(self) -> None:
        if not self.session.strip() or not self.turn.strip():
            raise EventError("A completion event requires session and turn identities")
        if self.kind is Kind.END and (self.report is None or not self.report.strip()):
            raise EventError("A completed native turn requires its final response")
        if self.kind is not Kind.END and self.report is not None:
            raise EventError("Only a completed native turn can supply a final response")

    def document(self) -> dict[str, str | None]:
        return {
            "session": self.session,
            "turn": self.turn,
            "event": self.kind.value,
            "report": self.report,
            "pending": self.pending,
        }


class Events(Protocol):
    def rejected_prompt(self, reason: str) -> dict[str, str]: ...

    def parse(self, payload: Mapping[str, object]) -> Event | None: ...
