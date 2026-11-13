"""Pin the `tj item` CLI and install the work-item profiles its plugins declare."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tj_agent.host import Host

PINS = Path(__file__).resolve().with_name("work-items.json")
MARKETPLACE_LOCK = (
    Path(__file__).resolve().parents[1]
    / "utility"
    / "plugins"
    / "scripts"
    / "marketplace_plugins.lock.json"
)
DECLARATION_NAME = "work-items.json"
RECEIPT = "uv-receipt.toml"


class WorkItemsError(RuntimeError):
    pass


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

    @classmethod
    def load(cls, path: Path = PINS) -> Pins:
        document = json.loads(path.read_text(encoding="utf-8"))
        if set(document) != {"tool"}:
            raise WorkItemsError(f"{path}: must contain exactly tool")
        return cls(Pin(**document["tool"]))


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def repository_names(path: Path | None = None) -> dict[str, str]:
    """Map each marketplace plugin name to its `tj-agents` GitHub repository."""
    marketplace = _read_json(MARKETPLACE_LOCK if path is None else path).get(
        "marketplace"
    )
    entries = marketplace.get("plugins") if isinstance(marketplace, dict) else None
    if not isinstance(entries, list):
        return {}
    return {
        entry["name"]: f"tj-agents/{entry['repository']}"
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and isinstance(entry.get("repository"), str)
    }


def _declared_profile(root: Path) -> tuple[str, str] | None:
    """A plugin's own work-item declaration, when its installed payload ships one."""
    document = _read_json(root / DECLARATION_NAME)
    package, subdirectory = document.get("package"), document.get("subdirectory")
    if (
        document.get("schema_version") != 1
        or not isinstance(package, str)
        or not isinstance(subdirectory, str)
    ):
        return None
    return package, subdirectory


def discovered_profiles(
    claude_config: Path | None = None, repositories: Mapping[str, str] | None = None
) -> tuple[Pin, ...]:
    """Work-item profile packages this machine's installed plugins declare.

    A specific profile plugin ships its package and subdirectory in its own
    installed payload (`work-items.json`); the repository comes from the
    marketplace snapshot Agent already carries, and the revision is the exact
    commit Claude installed that plugin from, so no separate pin needs renewal
    here. Only Claude's installed-plugin inventory records that revision today;
    a profile enabled only in Codex is not discovered.
    """
    claude_config = (
        Host.CLAUDE.configuration_root if claude_config is None else claude_config
    )
    repositories = repository_names() if repositories is None else repositories
    settings = _read_json(claude_config / "settings.json").get("enabledPlugins")
    registry = _read_json(claude_config / "plugins" / "installed_plugins.json").get(
        "plugins"
    )
    if not isinstance(settings, dict) or not isinstance(registry, dict):
        return ()
    found: dict[str, Pin] = {}
    for identity, enabled in settings.items():
        if enabled is not True:
            continue
        entries = registry.get(identity)
        if not isinstance(entries, list):
            continue
        name = identity.split("@", 1)[0]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            install_path, revision = entry.get("installPath"), entry.get("gitCommitSha")
            if not isinstance(install_path, str) or not isinstance(revision, str):
                continue
            declared = _declared_profile(Path(install_path))
            if declared is None:
                continue
            repository = repositories.get(name)
            if repository is None:
                raise WorkItemsError(
                    f"{identity} declares a work-items profile but {name} is not a "
                    "known marketplace plugin"
                )
            found[identity] = Pin(declared[0], repository, declared[1], revision)
            break
    return tuple(found[identity] for identity in sorted(found))


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


def drift(tool_directory: Path, pins: Pins, profiles: Sequence[Pin]) -> list[str]:
    wanted = {pin.package: pin for pin in (pins.tool, *profiles)}
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
