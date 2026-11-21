"""A reporting relationship belongs only to an explicitly parented handoff."""

from dataclasses import dataclass
from typing import ClassVar

from launch_errors import LaunchError

from tj_agent.host import Host


@dataclass(frozen=True, slots=True)
class IndependentHandoff:
    parent: ClassVar[None] = None

    @staticmethod
    def prompt(prepared: str) -> str:
        return prepared

    @staticmethod
    def receipt() -> dict[str, str]:
        return {}


@dataclass(frozen=True, slots=True)
class ParentedHandoff:
    parent: str

    def __post_init__(self) -> None:
        host, _, identity = self.parent.partition(":")
        if (
            host not in {entry.value for entry in Host}
            or not identity
            or any(char.isspace() or char == ":" for char in identity)
        ):
            raise LaunchError("--parent requires a Claude or Codex host:id handle")

    def prompt(self, prepared: str) -> str:
        return (
            f"{prepared} Standing rule: use tj message to contact {self.parent} only "
            "for a blocker you cannot resolve or an approval you need. At completion "
            "request tj finish and give the final response. Managed completion sends "
            "that response to the recorded parent once."
        )

    def receipt(self) -> dict[str, str]:
        return {"parent": self.parent}


def relationship(parent: str | None) -> IndependentHandoff | ParentedHandoff:
    return IndependentHandoff() if parent is None else ParentedHandoff(parent)
