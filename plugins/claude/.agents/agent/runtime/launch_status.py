#!/usr/bin/env python3
"""Share one launch attempt's startup state between a terminal launcher and the tab it opened."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Mapping

REPLACE_ATTEMPTS = 40
REPLACE_DELAY_SECONDS = 0.05


def read(path: Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _temporary(path: Path, value: Mapping[str, object]) -> Path:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    temporary.write_text(json.dumps(dict(value)), encoding="utf-8")
    return temporary


def create(path: Path, value: Mapping[str, object]) -> bool:
    path = Path(path)
    temporary = _temporary(path, value)
    try:
        os.link(temporary, path)
        return True
    except FileExistsError:
        return False
    except OSError:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def replace(path: Path, value: Mapping[str, object]) -> bool:
    path = Path(path)
    temporary = _temporary(path, value)
    try:
        for _attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, path)
                return True
            except PermissionError:
                time.sleep(REPLACE_DELAY_SECONDS)
        return False
    finally:
        temporary.unlink(missing_ok=True)
