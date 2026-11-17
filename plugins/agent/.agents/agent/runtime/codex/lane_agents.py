"""Deliver Agent's lane agents into the Codex profile (Python 3.12+).

Codex cannot load an agent from a plugin, so `tj install` copies the packaged
`lane-l1.toml` to `lane-l7.toml` into `<CODEX_HOME>/agents` and records the digests it
wrote in an ownership file there. It never overwrites or removes a file it does not own;
a file already holding the packaged bytes is adopted.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

PACKAGED = Path(".agents") / "agent" / "resources" / "lanes" / "agents" / "codex"
OWNERSHIP = ".tj-agents-lanes.json"
OWNER = "tj-agents/agent"


class LaneAgentError(RuntimeError):
    pass


@dataclass(frozen=True)
class Change:
    action: str
    path: Path

    def __str__(self) -> str:
        return f"{self.action} {self.path}"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def packaged(root: Path) -> dict[str, bytes]:
    directory = root / PACKAGED
    agents = {path.name: path.read_bytes() for path in directory.glob("lane-*.toml")}
    if not agents:
        raise LaneAgentError(f"no packaged Codex lane agents under {directory}")
    return agents


def target(profile: Path) -> Path:
    directory = profile / "agents"
    if directory.is_symlink() or directory.is_junction():
        raise LaneAgentError(
            f"refusing to deliver lane agents through a link: {directory}"
        )
    return directory


def owned(directory: Path) -> dict[str, str]:
    path = directory / OWNERSHIP
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LaneAgentError(
            f"unreadable lane agent ownership {path}: {error}"
        ) from error
    files = value.get("files") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("owner") != OWNER
        or not isinstance(files, dict)
        or not all(
            isinstance(name, str)
            and Path(name).name == name
            and name.endswith(".toml")
            and isinstance(recorded, str)
            for name, recorded in files.items()
        )
    ):
        raise LaneAgentError(f"invalid lane agent ownership {path}")
    return files


def current(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise LaneAgentError(f"Codex agent is not a regular file: {path}")
    return digest(path.read_bytes())


def plan(root: Path, profile: Path) -> list[Change]:
    """The changes that make the profile's lane agents match the package, in order."""
    directory = target(profile)
    agents = packaged(root)
    records = owned(directory)
    changes = []
    for name, data in sorted(agents.items()):
        path = directory / name
        found = current(path)
        if found is None:
            changes.append(Change("add", path))
        elif found == digest(data):
            if records.get(name) != found:
                changes.append(Change("adopt", path))
        elif records.get(name) == found:
            changes.append(Change("replace", path))
        else:
            raise LaneAgentError(
                f"refusing to overwrite {path}: tj install did not deliver it; "
                "move it aside and rerun"
            )
    for name, recorded in sorted(records.items()):
        if name in agents:
            continue
        path = directory / name
        found = current(path)
        if found not in (None, recorded):
            raise LaneAgentError(
                f"refusing to remove the modified retired lane agent {path}; "
                "move it aside and rerun"
            )
        changes.append(Change("remove", path))
    return changes


def check(root: Path, profile: Path) -> list[str]:
    return [str(change) for change in plan(root, profile)]


def write(path: Path, data: bytes) -> None:
    staged = path.with_name(f".{path.name}.tj-agents.tmp")
    staged.write_bytes(data)
    staged.replace(path)


def apply(root: Path, profile: Path) -> list[str]:
    changes = plan(root, profile)
    agents = packaged(root)
    directory = target(profile)
    directory.mkdir(parents=True, exist_ok=True)
    for change in changes:
        if change.action in {"add", "replace"}:
            write(change.path, agents[change.path.name])
        elif change.action == "remove":
            change.path.unlink(missing_ok=True)
    ownership = {
        "schema_version": 1,
        "owner": OWNER,
        "files": {name: digest(data) for name, data in sorted(agents.items())},
    }
    write(directory / OWNERSHIP, (json.dumps(ownership, indent=2) + "\n").encode())
    return [str(change) for change in changes]
