"""Native plugin refresh primitives shared by every host (Python 3.9+)."""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path
import subprocess

import plugin_autoupdate

TIMEOUT = 300
IN_USE = ".in_use"


class RefreshError(RuntimeError):
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


def run(command: list[str], project: Path) -> str:
    result = subprocess.run(command, cwd=project, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=TIMEOUT)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip() or f"Command exited {result.returncode}"
        raise RefreshError(f"{' '.join(command[1:])}: {detail}")
    return result.stdout


@functools.lru_cache(maxsize=None)
def resolved(path: object) -> Path:
    return Path(path).resolve()


def path_key(path: object) -> str:
    return os.path.normcase(str(resolved(path)))


def package_tree(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        raise RefreshError(f"Missing plugin directory: {root}")
    result = {}
    for path in root.rglob("*"):
        parts = path.relative_to(root).parts
        if parts[0] == IN_USE:
            continue
        if path.is_symlink():
            raise RefreshError(f"Unexpected plugin symlink: {path}")
        if path.is_file() and ".git" not in parts:
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result
