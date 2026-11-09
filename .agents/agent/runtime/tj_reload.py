"""tj reload: bring back every agent of every open work item, one item's, or one session.

A Claude background session is respawned in place; any other dead agent resumes in a new `tj run` tab in
its recorded worktree, keeping its title, item and parent. Live agents are left alone, and sessions that
ended cleanly stay ended unless `--ended` asks for them too.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tj_agent.host import Host
import agent_cli
import agent_store
import lifecycle


def plan(items: list[lifecycle.Item], ended: bool = False) -> list[tuple[str, lifecycle.Agent]]:
    selected = []
    for item in items:
        for agent in item.agents:
            if agent.alive or (agent.session["state"] == "ended" and not ended):
                continue
            selected.append((item.ref, agent))
    return selected


def restore(item: str | None, agent: lifecycle.Agent) -> dict:
    session = agent.session
    adapter = agent_cli.host_launch(agent.host)
    if agent.live is not None and agent.live.background:
        adapter.respawn(agent.live.background)
        return {"session": session["session"], "action": "respawned"}
    directory = Path(session.get("worktree") or session["cwd"])
    if not directory.is_dir():
        raise agent_cli.LaunchError(f"{session['session']}'s directory {directory} no longer exists")
    title = session.get("title") or session["session"]
    shown = agent_cli.launch(agent.host, directory, title,
                             adapter.resume_arguments(directory, title, session["native_id"]),
                             item=item, parent=session.get("parent"))
    return {"session": session["session"], "action": "resumed", "launch_id": shown["launch"]["launch_id"]}


def single(handle: str) -> tuple[None, lifecycle.Agent]:
    session = agent_store.run("show", handle)["session"]
    host = Host(session["host"])
    return None, lifecycle.Agent(session, agent_cli.host_launch(host).liveness().get(session["native_id"]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tj reload", description=__doc__)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("item", nargs="?", help="one work item; default: every open item")
    target.add_argument("--session", help="one session, as host:session-id")
    parser.add_argument("--ended", action="store_true", help="also bring back sessions that ended cleanly")
    parser.add_argument("--dry-run", action="store_true", help="list what would be restored")
    args = parser.parse_args(argv)
    try:
        if args.session:
            selected = [single(args.session)]
            if selected[0][1].alive:
                selected = []
        else:
            selected = plan(lifecycle.open_items([args.item] if args.item else None, cwd=Path.cwd()), args.ended)
    except (lifecycle.LifecycleError, agent_store.AgentStoreError, ValueError) as error:
        print(f"tj reload: {error}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(json.dumps([{"item": item, "session": agent.session["session"], "status": agent.status}
                          for item, agent in selected], indent=2))
        return 0
    results, failed = [], False
    for item, agent in selected:
        try:
            results.append(restore(item, agent))
        except (agent_cli.LaunchError, agent_store.AgentStoreError) as error:
            failed = True
            results.append({"session": agent.session["session"], "action": "failed", "error": str(error)})
    print(json.dumps(results, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
