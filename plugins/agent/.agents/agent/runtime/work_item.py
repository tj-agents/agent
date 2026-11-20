"""Resolve existing work-item references for session links."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from launch_errors import LaunchError
from tj_cli import process

COMMAND_TIMEOUT_SECONDS = 30


def reference(
    cwd: Path,
    value: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = process.run,
) -> str:
    """Resolve a bare id through the project's tracker without a network lookup."""
    value = value.strip()
    if not value:
        raise LaunchError(
            "--item is empty; pass an existing work-item reference or omit --item."
        )
    if ":" in value:
        return value
    tj = shutil.which("tj")
    if not tj:
        raise LaunchError("tj is not on PATH; run the Agent install.")
    command = [tj, "item", "--cwd", str(cwd), "reference", value]
    try:
        result = run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise LaunchError(f"tj item reference could not run: {error}") from error
    if result.returncode != 0:
        raise LaunchError(
            f"tj item reference failed: {(result.stderr or result.stdout).strip()}"
        )
    try:
        document = json.loads(result.stdout)
    except ValueError as error:
        raise LaunchError(
            f"tj item reference printed no JSON: {result.stdout.strip()}"
        ) from error
    ref = document.get("ref") if isinstance(document, dict) else None
    if not isinstance(ref, str) or not ref:
        raise LaunchError(
            f'tj item reference printed no "ref": {result.stdout.strip()}'
        )
    return ref
