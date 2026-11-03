"""Native plugin refresh primitives shared by every host (Python 3.9+)."""

from __future__ import annotations

import functools
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar

import plugin_autoupdate
from tj_cli import process

TIMEOUT = 300
IN_USE = ".in_use"
_maintenance_deadline: ContextVar[float | None] = ContextVar("maintenance_deadline", default=None)


class RefreshError(RuntimeError):
    pass


class MaintenanceUnsettledError(RefreshError):
    pass


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise RefreshError(f"Expected an object in {path}")
    return value


def repository(source: object) -> str | None:
    return plugin_autoupdate.github_repository(source)


def tj_agents(repository: str | None) -> bool:
    return bool(repository) and repository.split("/", 1)[0] == "tj-agents"


def selected(sources: dict[str, str | None], enabled: list[str]) -> tuple[dict[str, str], list[str]]:
    sources = {name: repo for name, repo in sources.items() if tj_agents(repo)}
    enabled = sorted(name for name in enabled if name.rsplit("@", 1)[-1] in sources)
    used = {name.rsplit("@", 1)[-1] for name in enabled}
    marketplaces = {name: repo for name, repo in sorted(sources.items()) if name in used}
    for name, repo in marketplaces.items():
        error = plugin_autoupdate.marketplace_error(name, repo)
        if error:
            raise RefreshError(error)
    return marketplaces, enabled


@contextmanager
def maintenance_budget(seconds: float):
    deadline = time.monotonic() + seconds
    token = _maintenance_deadline.set(deadline)
    try:
        yield deadline
    finally:
        _maintenance_deadline.reset(token)


def remaining(deadline: float | None = None) -> float | None:
    deadline = _maintenance_deadline.get() if deadline is None else deadline
    return None if deadline is None else deadline - time.monotonic()


def _run_bounded(command: list[str], project: Path, deadline: float,
                 timeout: float, inherit: bool) -> subprocess.CompletedProcess:
    remaining_time = remaining(deadline)
    if remaining_time is None or remaining_time <= 0:
        raise RefreshError(f"{' '.join(command[1:])}: maintenance budget expired")
    timeout = min(timeout, remaining_time)
    try:
        return process.run(
            command,
            cwd=project,
            capture_output=not inherit,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise MaintenanceUnsettledError(
            f"{' '.join(command[1:])}: maintenance writer settlement could not be proven"
        ) from None


def execute(command: list[str], project: Path, *, timeout: float = TIMEOUT,
            creationflags: int = 0, inherit: bool = False) -> subprocess.CompletedProcess:
    deadline = _maintenance_deadline.get()
    if deadline is not None:
        return _run_bounded(command, project, deadline, timeout, inherit)
    if inherit:
        return subprocess.run(command, timeout=timeout, check=False)
    options = {
        "cwd": project,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if creationflags:
        options["creationflags"] = creationflags
    return subprocess.run(command, **options)


def run(command: list[str], project: Path, *, deadline: float | None = None) -> str:
    if deadline is not None:
        token = _maintenance_deadline.set(deadline)
        try:
            result = execute(command, project)
        finally:
            _maintenance_deadline.reset(token)
    else:
        result = execute(command, project)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip() or f"Command exited {result.returncode}"
        raise RefreshError(f"{' '.join(command[1:])}: {detail}")
    return result.stdout


@functools.lru_cache(maxsize=None)
def resolved(path: object) -> Path:
    return Path(path).resolve()


def path_key(path: object) -> str:
    return os.path.normcase(str(resolved(path)))


def generated_cache(path: Path) -> bool:
    if not path.is_file() or path.parent.name != "__pycache__" or path.suffix != ".pyc":
        return False
    try:
        importlib.util.source_from_cache(str(path))
    except ValueError:
        return False
    return True


def package_tree(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise RefreshError(f"Unexpected plugin link: {root}")
    if not root.is_dir():
        raise RefreshError(f"Missing plugin directory: {root}")
    result = {}
    for path in root.rglob("*"):
        parts = path.relative_to(root).parts
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise RefreshError(f"Unexpected plugin symlink: {path}")
        if parts[0] == IN_USE or generated_cache(path):
            continue
        if path.is_file() and ".git" not in parts:
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result
