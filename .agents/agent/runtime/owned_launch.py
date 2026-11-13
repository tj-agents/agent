"""The `tj run` launch that owns the calling process, as `tj launch` reports it."""

from __future__ import annotations

import subprocess

from tj_cli import process

# A loaded machine can take seconds to start `tj`; allow what an agent-store call gets.
TIMEOUT_SECONDS = 20


class OwnedLaunchError(Exception):
    """`tj launch` did not answer, so whether a launch owns this process is unknown."""


def current() -> str | None:
    """The owning launch id, or None when `tj launch` answers that none owns us."""
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
    except (OSError, subprocess.SubprocessError) as error:
        raise OwnedLaunchError(f"tj launch did not answer: {error}") from error
    if result.returncode:
        raise OwnedLaunchError(
            f"tj launch exited {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout.strip() or None
