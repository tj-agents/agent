import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import assert_never

import agent_cli
import agent_store
from launch_contract import Liveness
from launch_errors import LaunchError
from tj_agent import worktrees
from tj_agent.host import Host


class Command(StrEnum):
    CLAIM_WORKTREE = "claim-worktree"
    LIVENESS = "liveness"


class LivenessError(RuntimeError):
    pass


def require_binding(host: Host, native_id: str, launch_id: str) -> None:
    session = f"{host}:{native_id}"
    current = agent_store.run("show", session)
    bound = agent_store.run("show", "--launch-id", launch_id)
    if not isinstance(current, dict) or not isinstance(bound, dict):
        raise LivenessError("agent-store returned an invalid binding response")
    expected = {
        "session": session,
        "host": host.value,
        "native_id": native_id,
        "launch_id": launch_id,
    }
    for row in (current.get("session"), bound.get("session")):
        if not isinstance(row, dict) or any(
            row.get(key) != value for key, value in expected.items()
        ):
            raise LivenessError("The current native session does not match the launch")
    launch = bound.get("launch")
    if not isinstance(launch, dict) or any(
        launch.get(key) != expected[key] for key in ("session", "host", "launch_id")
    ):
        raise LivenessError("The launch does not reciprocally bind the native session")


def session_liveness(session: str, launch_id: str) -> dict[str, str]:
    host_name, separator, native_id = session.partition(":")
    if (
        not separator
        or not native_id
        or ":" in native_id
        or any(character.isspace() for character in session)
        or not launch_id
        or any(character.isspace() for character in launch_id)
    ):
        raise LivenessError("An exact host:id and launch id are required")
    host = Host(host_name)
    require_binding(host, native_id, launch_id)
    inventory = agent_cli.host_launch(host).liveness(strict=True)
    if not isinstance(inventory, dict) or any(
        not isinstance(key, str)
        or not key
        or not isinstance(value, Liveness)
        or not isinstance(value.alive, bool)
        for key, value in inventory.items()
    ):
        raise LivenessError("The native host returned an invalid liveness inventory")
    # Strict adapters distinguish an exhausted native inventory from a failed
    # probe. Codex inventories loaded daemon threads, including idle threads;
    # wrapper exit alone is never evidence that a conversation is unloaded.
    observed = inventory.get(native_id)
    # A concurrent resume can replace the launch/session binding during the probe.
    require_binding(host, native_id, launch_id)
    return {
        "session": session,
        "launch_id": launch_id,
        "state": "live" if observed is not None and observed.alive else "dead",
    }


def report_liveness(session: str, launch_id: str) -> int:
    try:
        result = session_liveness(session, launch_id)
    except (
        OSError,
        subprocess.SubprocessError,
        RuntimeError,
        ValueError,
        LaunchError,
    ) as error:
        print(f"tj session liveness: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tj session", description="Inspect session liveness and record ownership"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    claim = commands.add_parser(Command.CLAIM_WORKTREE)
    claim.add_argument("--path", type=Path, required=True)
    liveness = commands.add_parser(Command.LIVENESS)
    liveness.add_argument("--session", required=True, metavar="HOST:ID")
    liveness.add_argument("--launch-id", required=True)
    arguments = parser.parse_args(argv)
    match Command(arguments.command):
        case Command.LIVENESS:
            return report_liveness(arguments.session, arguments.launch_id)
        case Command.CLAIM_WORKTREE:
            pass
        case other:
            assert_never(other)
    try:
        owner = worktrees.current_owner(os.environ)
        ownership = worktrees.claim(arguments.path, owner) if owner else None
    except (OSError, worktrees.OwnershipError, agent_store.AgentStoreError) as error:
        print(f"tj session claim-worktree: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"ownership": ownership.document() if ownership else None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
