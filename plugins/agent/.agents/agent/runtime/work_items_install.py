"""Pin the `tj item` CLI and the work-item profiles this machine selects."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from claude import installed as claude_installed
from codex import installed as codex_installed
from tj_agent.host import Host

PINS = Path(__file__).resolve().with_name("work-items.json")
RECEIPT = "uv-receipt.toml"


class Installed(Protocol):
    def identities(self) -> set[str]: ...


INSTALLED: Mapping[Host, Installed] = {
    Host.CLAUDE: claude_installed,
    Host.CODEX: codex_installed,
}


@dataclass(frozen=True, slots=True)
class Pin:
    package: str
    repository: str
    subdirectory: str
    revision: str

    @property
    def requirement(self) -> str:
        return (
            f"git+https://github.com/{self.repository}@{self.revision}"
            f"#subdirectory={self.subdirectory}"
        )


@dataclass(frozen=True, slots=True)
class Pins:
    tool: Pin
    profiles: Mapping[str, Pin]

    @classmethod
    def load(cls, path: Path = PINS) -> Pins:
        document = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            Pin(**document["tool"]),
            {plugin: Pin(**pin) for plugin, pin in document["profiles"].items()},
        )

    def selected(self, plugins: Iterable[str]) -> tuple[Pin, ...]:
        present = set(plugins)
        return tuple(
            pin for plugin, pin in sorted(self.profiles.items()) if plugin in present
        )


def installed_plugins() -> set[str]:
    found = set()
    for host in Host:
        found.update(INSTALLED[host].identities())
    return found


def default_tool_directory(environ: Mapping[str, str], windows: bool) -> Path:
    if configured := environ.get("UV_TOOL_DIR"):
        return Path(configured)
    if windows:
        return Path(environ["APPDATA"]) / "uv" / "tools"
    data = environ.get("XDG_DATA_HOME")
    return (Path(data) if data else Path.home() / ".local" / "share") / "uv" / "tools"


def install_command(uv: str, pins: Pins, profiles: Sequence[Pin]) -> list[str]:
    extras: list[str] = []
    for pin in profiles:
        extras.extend(("--with", pin.requirement))
    return [
        uv,
        "tool",
        "install",
        "--force",
        "--from",
        pins.tool.requirement,
        *extras,
        pins.tool.package,
    ]


def _receipt_pin(requirement: Mapping[str, object]) -> Pin:
    source = urlsplit(str(requirement.get("git", "")))
    query = parse_qs(source.query)
    repository = source.path.strip("/").removesuffix(".git")
    if source.netloc != "github.com":
        repository = source.geturl()
    return Pin(
        str(requirement["name"]),
        repository,
        query.get("subdirectory", [""])[0],
        query.get("rev", [""])[0],
    )


def receipt(path: Path) -> dict[str, Pin]:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    tool = document.get("tool")
    requirements = tool.get("requirements") if isinstance(tool, dict) else None
    if not isinstance(requirements, list):
        return {}
    return {
        entry["name"]: _receipt_pin(entry)
        for entry in requirements
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }


def drift(tool_directory: Path, pins: Pins, plugins: Iterable[str]) -> list[str]:
    wanted = {pin.package: pin for pin in (pins.tool, *pins.selected(plugins))}
    actual = receipt(tool_directory / pins.tool.package / RECEIPT)
    found = []
    for package in sorted(wanted.keys() | actual.keys()):
        pin, present = wanted.get(package), actual.get(package)
        if present is None:
            found.append(f"{package} is not installed")
        elif pin is None:
            found.append(f"{package} is installed but no profile selects it")
        elif present != pin:
            found.append(
                f"{package} is {present.repository}@{present.revision[:12]}, "
                f"pinned {pin.repository}@{pin.revision[:12]}"
            )
    return found
