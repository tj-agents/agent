"""Shared errors and file helpers for the plugin-selection scripts."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any


class LockError(RuntimeError):
    """A user-visible validation or generation failure."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LockError(f"Cannot read JSON at {path}: {error}") from error
    if not isinstance(value, dict):
        raise LockError(f"Expected a JSON object at {path}")
    return value


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LockError(f"{label} must be a non-empty string")
    return value


def require_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise LockError(f"{label} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise LockError(f"{label} contains duplicates")
    return value


def executable(name: str) -> str:
    located = shutil.which(name)
    if not located:
        raise LockError(f"Required executable was not found on PATH: {name}")
    return located


def git(run, arguments: list[str], cwd: Path | None = None) -> str:
    completed = run(
        [executable("git"), *arguments], cwd=cwd, text=True, encoding="utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise LockError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()
