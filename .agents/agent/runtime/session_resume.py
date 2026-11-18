"""Admit a stopped managed conversation before opening its replacement wrapper."""

from __future__ import annotations

import json
import subprocess

import agent_store
from launch_errors import LaunchError
from tj_cli import process


def predecessor(session: str, expected: str | None = None) -> str | None:
    try:
        document = agent_store.run("show", session)
    except agent_store.AgentStoreError as error:
        if error.code == "not-found" and expected is None:
            return None
        raise LaunchError(f"Cannot inspect the resumed session: {error}") from error
    record = document.get("session")
    if not isinstance(record, dict) or record.get("session") != session:
        raise LaunchError("agent-store returned a different resumed session")
    launch_id = record.get("launch_id")
    if expected is not None and launch_id != expected:
        raise LaunchError("The session was rebound after recovery was planned")
    if launch_id is not None and (not isinstance(launch_id, str) or not launch_id):
        raise LaunchError("agent-store returned an invalid predecessor launch")
    return launch_id


def check(session: str, launch_id: str) -> None:
    try:
        result = process.run(
            ["tj", "_resume-check", "--session", session, "--launch-id", launch_id],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
            check=False,
        )
        if result.returncode:
            raise LaunchError(result.stderr.strip() or "Recovery admission failed")
        proof = json.loads(result.stdout)
        if (
            not isinstance(proof, dict)
            or proof.get("session") != session
            or proof.get("launch_id") != launch_id
        ):
            raise LaunchError("Recovery admission returned a different owner")
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise LaunchError(f"Cannot verify recovery admission: {error}") from error


def bind_started(session: str, launch_id: str) -> dict[str, object] | None:
    """Transfer a prior owner from the new native process's verified startup hook."""
    try:
        prior = predecessor(session)
        if prior is None or prior == launch_id:
            return None
        result = process.run(
            [
                "tj",
                "_resume-bind",
                "--session",
                session,
                "--launch-id",
                launch_id,
                "--from-launch-id",
                prior,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
            check=False,
        )
        if result.returncode:
            raise LaunchError(
                result.stderr.strip() or "Native resume admission failed"
            )
        document = json.loads(result.stdout)
        record = document.get("session") if isinstance(document, dict) else None
        if (
            not isinstance(record, dict)
            or record.get("session") != session
            or record.get("launch_id") != launch_id
            or record.get("state") != "active"
        ):
            raise LaunchError("Native resume returned a different owner")
    except (OSError, subprocess.SubprocessError, ValueError, LaunchError) as error:
        raise LaunchError(f"Cannot bind native resume: {error}") from error
    return record
