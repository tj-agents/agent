"""Recover dead Claude and Codex sessions from the previous uptime."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_cli
import agent_store
import boot_history
import lifecycle
from host_sessions import SESSIONS
from launch_contract import Liveness
from tj_agent.host import Host

STALE_REASON = "recovery-stale"


class Action(StrEnum):
    RESUME = "resume"
    RESPAWN = "respawn"
    END = "end"
    FAILED = "failed"
    ENDED = "ended"
    RESUMED = "resumed"
    RESPAWNED = "respawned"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class Recovery:
    session: str
    item: str | None
    title: str
    parent: str | None
    launch_id: str | None
    directory: str
    action: Action = Action.RESUME
    modified_at: float | None = None
    background: str | None = None
    error: str | None = None
    previous_launch_id: str | None = None
    open_launches: tuple[str, ...] = ()

    @property
    def host(self) -> Host:
        return Host(self.session.split(":", 1)[0])


def sessions(item: str | None, handle: str | None) -> list[dict[str, Any]]:
    if handle:
        return [agent_store.run("show", handle)["session"]]
    return cast(
        list[dict[str, Any]],
        agent_store.run(
            "sessions",
            *(["--item", item] if item else []),
        )["sessions"],
    )


def liveness(
    hosts: set[Host],
) -> tuple[dict[Host, dict[str, Liveness]], dict[Host, str]]:
    live: dict[Host, dict[str, Liveness]] = {}
    errors: dict[Host, str] = {}
    for host in hosts:
        try:
            live[host] = agent_cli.host_launch(host).liveness(strict=True)
        except (agent_cli.LaunchError, OSError, RuntimeError) as error:
            live[host] = {}
            errors[host] = str(error)
    return live, errors


def open_plan(item: str | None) -> list[Recovery]:
    launches = [
        launch
        for launch in agent_store.run("launches", *(["--item", item] if item else []))[
            "launches"
        ]
        if launch.get("open")
    ]
    by_session: dict[str, list[dict[str, Any]]] = {}
    results = []
    for launch in launches:
        if launch.get("session"):
            by_session.setdefault(launch["session"], []).append(launch)
        else:
            results.append(
                Recovery(
                    session="",
                    item=(launch.get("item") or {}).get("ref"),
                    title=launch["title"],
                    parent=launch.get("parent"),
                    launch_id=launch["launch_id"],
                    directory=launch.get("worktree") or launch["cwd"],
                    action=Action.FAILED,
                    error=f"launch {launch['launch_id']} has no session to resume",
                )
            )
    sessions = {
        handle: agent_store.run("show", handle)["session"] for handle in by_session
    }
    live, errors = liveness({Host(session["host"]) for session in sessions.values()})
    for handle, opened in by_session.items():
        session = sessions[handle]
        host = Host(session["host"])
        agent = lifecycle.Agent(session, live[host].get(session["native_id"]))
        if agent.alive:
            continue
        launch = opened[-1]
        item = launch.get("item")
        row = Recovery(
            session=handle,
            item=item["ref"] if item else None,
            title=launch["title"],
            parent=launch.get("parent"),
            launch_id=launch["launch_id"],
            directory=launch.get("worktree") or launch["cwd"],
            open_launches=tuple(each["launch_id"] for each in opened),
        )
        if host in errors:
            row = replace(row, action=Action.FAILED, error=errors[host])
        elif not Path(row.directory).is_dir():
            row = replace(
                row,
                action=Action.FAILED,
                error=f"Directory no longer exists: {row.directory}",
            )
        elif agent.live and agent.live.background:
            row = replace(row, action=Action.RESPAWN, background=agent.live.background)
        results.append(row)
    return results


def plan(
    records: list[dict[str, Any]],
    older: bool,
    ended: bool,
) -> list[Recovery]:
    candidates = [
        session
        for session in records
        if ended
        or session["state"] != "ended"
        or (older and session.get("end_reason") == STALE_REASON)
    ]
    live, errors = liveness({Host(session["host"]) for session in candidates})
    agents = [
        lifecycle.Agent(session, live[Host(session["host"])].get(session["native_id"]))
        for session in candidates
    ]
    dead = [agent for agent in agents if not agent.alive]
    cutoff = (
        None
        if older or not any(agent.host not in errors for agent in dead)
        else boot_history.previous_boot()
    )
    launches = {
        row["launch_id"]: row for row in agent_store.run("launches")["launches"]
    }
    results = []
    for agent in dead:
        session, host = agent.session, agent.host
        launch = launches.get(session.get("launch_id"), {})
        links = session.get("links") or []
        item = launch.get("item") or (links[0]["item"] if links else None)
        row = Recovery(
            session=session["session"],
            item=item["ref"] if item else None,
            title=launch.get("title") or session.get("title") or session["session"],
            parent=launch.get("parent") or session.get("parent"),
            launch_id=session.get("launch_id"),
            directory=launch.get("worktree")
            or session.get("worktree")
            or launch.get("cwd")
            or session["cwd"],
            open_launches=(launch["launch_id"],) if launch.get("open") else (),
        )
        try:
            if host in errors:
                raise agent_cli.LaunchError(errors[host])
            if cutoff is not None:
                row = replace(
                    row,
                    modified_at=SESSIONS[host].transcript_modified(
                        session["native_id"]
                    ),
                )
            if (
                cutoff is not None
                and row.modified_at is not None
                and row.modified_at <= cutoff
            ):
                row = replace(
                    row,
                    action=Action.END if session["state"] != "ended" else Action.SKIP,
                )
            elif not Path(row.directory).is_dir():
                row = replace(
                    row,
                    action=Action.FAILED,
                    error=f"Directory no longer exists: {row.directory}",
                )
            else:
                if agent.live and agent.live.background:
                    row = replace(
                        row, action=Action.RESPAWN, background=agent.live.background
                    )
        except (agent_cli.LaunchError, OSError, ValueError, RuntimeError) as error:
            row = replace(row, action=Action.FAILED, error=str(error))
        if row.action != Action.SKIP:
            results.append(row)
    return results


def restore(row: Recovery) -> Recovery:
    handle = row.session
    if row.action == Action.END:
        agent_store.run("end", "--session", handle, "--reason", STALE_REASON)
        return replace(row, action=Action.ENDED)
    adapter = agent_cli.host_launch(row.host)
    if row.action == Action.RESPAWN and row.background:
        adapter.respawn(row.background)
        return replace(row, action=Action.RESPAWNED)
    directory = Path(row.directory)
    if not directory.is_dir():
        raise agent_cli.LaunchError(f"Directory no longer exists: {directory}")
    shown = agent_cli.launch(
        row.host,
        directory,
        row.title,
        adapter.resume_arguments(directory, row.title, handle.split(":", 1)[1]),
        item=row.item,
        parent=row.parent,
        want_session=True,
        tmux_session="tj-recover",
    )
    for launch_id in row.open_launches:
        agent_store.run("set-open", "--launch-id", launch_id, "--open", "false")
    return replace(
        row,
        action=Action.RESUMED,
        launch_id=shown["launch"]["launch_id"],
        previous_launch_id=row.launch_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tj recover", description=__doc__)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("item", nargs="?", help="limit recovery to one work item")
    target.add_argument("--session", help="limit recovery to one host:session-id")
    parser.add_argument(
        "--open",
        action="store_true",
        help="reopen exactly the tabs that were open, whose agent is not running",
    )
    parser.add_argument(
        "--older",
        action="store_true",
        help="also recover sessions older than the previous boot",
    )
    parser.add_argument(
        "--ended", action="store_true", help="also consider sessions marked ended"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list restores, auto-ends and errors without changes",
    )
    args = parser.parse_args(argv)
    if args.open and (args.session or args.older or args.ended):
        parser.error("--open cannot be combined with --session, --older or --ended")
    try:
        rows = (
            open_plan(args.item)
            if args.open
            else plan(sessions(args.item, args.session), args.older, args.ended)
        )
    except (
        lifecycle.LifecycleError,
        agent_store.AgentStoreError,
        agent_cli.LaunchError,
        OSError,
        ValueError,
    ) as error:
        print(f"tj recover: {error}", file=sys.stderr)
        return 1
    if not args.dry_run:
        for index, row in enumerate(rows):
            if row.action == Action.FAILED:
                continue
            try:
                rows[index] = restore(row)
            except (
                agent_cli.LaunchError,
                agent_store.AgentStoreError,
                OSError,
                ValueError,
            ) as error:
                rows[index] = replace(row, action=Action.FAILED, error=str(error))
    print(json.dumps([asdict(row) for row in rows], indent=2))
    if (
        not args.dry_run
        and sys.platform == "linux"
        and not os.environ.get("TMUX")
        and any(row.action == Action.RESUMED for row in rows)
    ):
        try:
            agent_cli.attach_tmux("tj-recover")
        except agent_cli.LaunchError as error:
            print(f"tj recover: {error}", file=sys.stderr)
            return 1
    return int(any(row.action == Action.FAILED for row in rows))


if __name__ == "__main__":
    sys.exit(main())
