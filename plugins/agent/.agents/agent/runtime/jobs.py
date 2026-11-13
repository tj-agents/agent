"""One machine-wide run of each job at a time, and its last outcome."""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator, Sequence
from enum import Enum
from pathlib import Path

STALE_SECONDS = 1800


class Job(Enum):
    INSTALL = ("machine-install", "machine install")

    def __init__(self, file_name: str, label: str) -> None:
        self.file_name = file_name
        self.label = label


def state_directory() -> Path:
    configured = os.environ.get("AGENT_STATE_DIRECTORY")
    return Path(configured) if configured else Path.home() / ".agents-state"


def lock_path(state: Path, job: Job) -> Path:
    return state / f"{job.file_name}.lock"


def status_path(state: Path, job: Job) -> Path:
    return state / f"{job.file_name}.json"


def running(state: Path, job: Job) -> bool:
    try:
        return time.time() - lock_path(state, job).stat().st_mtime < STALE_SECONDS
    except OSError:
        return False


@contextlib.contextmanager
def held(state: Path, job: Job) -> Iterator[bool]:
    lock = lock_path(state, job)
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and not running(state, job):
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


def record(state: Path, job: Job, failures: Sequence[str]) -> None:
    status = {"finished": time.time(), "failures": list(failures)}
    state.mkdir(parents=True, exist_ok=True)
    status_path(state, job).write_text(json.dumps(status), encoding="utf-8")
