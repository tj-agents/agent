"""The `tj run` launch that owns the calling process, as `tj launch` reports it."""

from __future__ import annotations

import subprocess

from tj_cli import process

TIMEOUT_SECONDS = 5


def current() -> str | None:
    try:
        result = process.run(
            ["tj", "launch"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    return result.stdout.strip() or None
