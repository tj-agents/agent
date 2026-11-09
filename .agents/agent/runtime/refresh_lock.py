"""One machine-wide plugin refresh at a time, and the outcome of the last one (Python 3.9+)."""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import json
import os
from pathlib import Path
import time

STALE_SECONDS = 1800
FAILURE_SECONDS = 600


def state_directory() -> Path:
    return Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state")


def lock_path() -> Path:
    return state_directory() / "plugin-refresh.lock"


def status_path() -> Path:
    return state_directory() / "plugin-refresh.json"


def running() -> bool:
    try:
        return time.time() - lock_path().stat().st_mtime < STALE_SECONDS
    except OSError:
        return False


def recent_failure() -> str | None:
    try:
        status = json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    failures = status.get("failures") if isinstance(status, dict) else None
    finished = status.get("finished", 0) if isinstance(status, dict) else 0
    if failures and isinstance(finished, (int, float)) and time.time() - finished < FAILURE_SECONDS:
        return str(failures[0])
    return None


@contextlib.contextmanager
def held() -> Iterator[bool]:
    lock = lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and not running():
        lock.unlink(missing_ok=True)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        yield False
        return
    try:
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        yield True
    finally:
        lock.unlink(missing_ok=True)


def record(failures: list[str]) -> None:
    status_path().write_text(json.dumps({"finished": time.time(), "failures": failures}), encoding="utf-8")
