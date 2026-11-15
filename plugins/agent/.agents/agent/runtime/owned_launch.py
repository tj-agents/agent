"""The `tj run` launch that owns the calling process, as `tj launch` reports it."""

from __future__ import annotations

import subprocess

import agent_store
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


def for_session(session: str) -> str | None:
    """Return the process's launch only if it can bind this native session."""
    launch_id = current()
    if launch_id is None:
        return None
    data = agent_store.run("show", "--launch-id", launch_id)
    launch = data.get("launch")
    if not isinstance(launch, dict) or launch.get("launch_id") != launch_id:
        raise OwnedLaunchError("agent-store returned an invalid owning launch")
    host, _, native_id = session.partition(":")
    launch_host = launch.get("host")
    if not isinstance(launch_host, str) or not launch_host:
        raise OwnedLaunchError("agent-store returned an invalid launch host")
    if launch_host != host:
        return None
    if launch.get("state") not in {"active", "closing"}:
        raise OwnedLaunchError("the owning launch is not active")
    bound = launch.get("session")
    if bound is None:
        return launch_id
    if not isinstance(bound, str) or not bound:
        raise OwnedLaunchError("agent-store returned an invalid launch session")
    registered = data.get("session")
    if bound != session:
        if (
            launch.get("state") == "active"
            and isinstance(registered, dict)
            and registered.get("session") == bound
            and registered.get("host") == host
            and registered.get("native_id") == bound.partition(":")[2]
            and registered.get("launch_id") == launch_id
            and registered.get("state") == "ended"
            and registered.get("end_reason") in {"clear", "resume"}
        ):
            return launch_id
        return None
    if (
        not isinstance(registered, dict)
        or registered.get("session") != session
        or registered.get("launch_id") != launch_id
        or registered.get("host") != host
        or registered.get("native_id") != native_id
    ):
        raise OwnedLaunchError("agent-store launch and native session do not match")
    return launch_id
