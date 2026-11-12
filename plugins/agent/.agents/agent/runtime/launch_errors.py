"""Failures at the native process and terminal launch boundary."""

import subprocess
from collections.abc import Callable


class LaunchError(Exception):
    pass


class LaunchTimeout(LaunchError):
    pass


def cleanup_failed_launch(
    original: BaseException | None, cleanup: Callable[[], object]
) -> None:
    try:
        cleanup()
    except (LaunchError, OSError, subprocess.SubprocessError, RuntimeError) as error:
        if original is None:
            raise
        original.add_note(f"Launch cleanup failed ({type(error).__name__}): {error}")
