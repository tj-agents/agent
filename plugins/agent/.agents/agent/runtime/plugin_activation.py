import hashlib
import sys
from pathlib import Path

from tj_agent.host import Host


def path(state: Path, host: Host, project: Path) -> Path:
    directory = project.resolve().as_posix()
    if sys.platform == "win32":
        directory = directory.casefold()
    identity = hashlib.sha256(directory.encode("utf-8")).hexdigest()
    return state / "plugin-activation" / str(host) / identity


def pending(state: Path, host: Host, project: Path) -> bool:
    return path(state, host, project).is_file()


def begin(state: Path, host: Host, project: Path) -> None:
    marker = path(state, host, project)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(project.resolve()), encoding="utf-8")


def complete(state: Path, host: Host, project: Path) -> None:
    path(state, host, project).unlink(missing_ok=True)
