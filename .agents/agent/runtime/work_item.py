"""Resolve or create the work item a handoff launch is linked to."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

import agent_store
from launch_errors import LaunchError

COMMAND_TIMEOUT_SECONDS = 30


def parent_item_ref(parent: str) -> str | None:
    """The ref of `parent`'s first linked item, owner role first, or None."""
    try:
        shown = agent_store.run("show", parent)
    except agent_store.AgentStoreError:
        return None
    links = shown["session"]["links"]
    if not links:
        return None
    owner = next((link for link in links if link["role"] == "owner"), None)
    ref = (owner or links[0])["item"]["ref"]
    return ref if isinstance(ref, str) else None


def create(
    cwd: Path,
    title: str,
    parent: str | None,
    fields: Mapping[str, str],
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Create a work item titled `title` for `cwd`, under `parent` if given."""
    tj = shutil.which("tj")
    if not tj:
        raise LaunchError("tj is not on PATH; run the Agent install.")
    command = [tj, "item", "--cwd", str(cwd), "create", "--title", title]
    if parent:
        command += ["--parent", parent]
    for name, value in fields.items():
        command += ["--field", f"{name}={value}"]
    try:
        result = run(
            command,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise LaunchError(f"tj item create could not run: {error}") from error
    if result.returncode != 0:
        raise LaunchError(
            f"tj item create failed: {(result.stderr or result.stdout).strip()}"
        )
    try:
        document = json.loads(result.stdout)
    except ValueError as error:
        raise LaunchError(
            f"tj item create printed no JSON: {result.stdout.strip()}"
        ) from error
    ref = document.get("ref") if isinstance(document, dict) else None
    if not isinstance(ref, str) or not ref:
        raise LaunchError(f'tj item create printed no "ref": {result.stdout.strip()}')
    return ref
