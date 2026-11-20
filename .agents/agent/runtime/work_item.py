"""Resolve session links and explicitly requested work-item creation."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Mapping
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
    return _command_ref(cwd, ["reference", value], run)


def existing(
    cwd: Path,
    value: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = process.run,
) -> str:
    """Resolve and verify a supplied tracker item before native dispatch."""
    ref = reference(cwd, value, run=run)
    return _command_ref(cwd, ["resolve", ref], run)


def create(
    cwd: Path,
    title: str,
    parent: str | None,
    fields: Mapping[str, str],
    run: Callable[..., subprocess.CompletedProcess[str]] = process.run,
) -> str:
    arguments = ["create", "--title", title]
    if parent:
        arguments += ["--parent", parent]
    for name, value in fields.items():
        arguments += ["--field", f"{name}={value}"]
    return _command_ref(cwd, arguments, run)


def _command_ref(
    cwd: Path,
    arguments: list[str],
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    operation = arguments[0]
    tj = shutil.which("tj")
    if not tj:
        raise LaunchError("tj is not on PATH; run the Agent install.")
    command = [tj, "item", "--cwd", str(cwd), *arguments]
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
        raise LaunchError(f"tj item {operation} could not run: {error}") from error
    if result.returncode != 0:
        raise LaunchError(
            f"tj item {operation} failed: {(result.stderr or result.stdout).strip()}"
        )
    try:
        document = json.loads(result.stdout)
    except ValueError as error:
        raise LaunchError(
            f"tj item {operation} printed no JSON: {result.stdout.strip()}"
        ) from error
    ref = document.get("ref") if isinstance(document, dict) else None
    if not isinstance(ref, str) or not ref.strip():
        raise LaunchError(
            f'tj item {operation} printed no "ref": {result.stdout.strip()}'
        )
    return ref
