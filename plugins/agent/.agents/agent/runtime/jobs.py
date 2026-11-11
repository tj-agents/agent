"""One machine-wide run of each detached job at a time, its log and its last outcome."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from enum import Enum
from pathlib import Path
from typing import TypedDict

from tj_cli import process

STALE_SECONDS = 1800
FAILURE_SECONDS = 600


class Job(Enum):
    UPDATE = ("plugin-update", "plugin update")
    INSTALL = ("machine-install", "machine install")

    def __init__(self, file_name: str, label: str) -> None:
        self.file_name = file_name
        self.label = label


class _Detached(TypedDict, total=False):
    creationflags: int
    start_new_session: bool


def state_directory() -> Path:
    configured = os.environ.get("AGENT_STATE_DIRECTORY")
    return Path(configured) if configured else Path.home() / ".agents-state"


def lock_path(state: Path, job: Job) -> Path:
    return state / f"{job.file_name}.lock"


def status_path(state: Path, job: Job) -> Path:
    return state / f"{job.file_name}.json"


def log_path(state: Path, job: Job) -> Path:
    return state / f"{job.file_name}.log"


def running(state: Path, job: Job) -> bool:
    try:
        return time.time() - lock_path(state, job).stat().st_mtime < STALE_SECONDS
    except OSError:
        return False


def recent_failure(state: Path, job: Job) -> str | None:
    try:
        status = json.loads(status_path(state, job).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(status, dict):
        return None
    failures = status.get("failures")
    finished = status.get("finished", 0)
    recent = (
        isinstance(finished, int | float) and time.time() - finished < FAILURE_SECONDS
    )
    if failures and isinstance(failures, list) and recent:
        return str(failures[0])
    return None


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


def attempt_path(state: Path, job: Job) -> Path:
    return state / f"{job.file_name}-attempt.json"


def attempted(state: Path, job: Job) -> object:
    try:
        return json.loads(attempt_path(state, job).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def attempt(state: Path, job: Job, drift: Sequence[str]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    attempt_path(state, job).write_text(json.dumps(list(drift)), encoding="utf-8")


def start(state: Path, job: Job, command: Sequence[str]) -> bool:
    log = log_path(state, job)
    log.parent.mkdir(parents=True, exist_ok=True)
    detached: _Detached
    if sys.platform == "win32":
        detached = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        detached = {"start_new_session": True}
    try:
        with log.open("ab") as output:
            process.start(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                close_fds=True,
                **detached,
            )
    except FileNotFoundError:
        return False
    return True
