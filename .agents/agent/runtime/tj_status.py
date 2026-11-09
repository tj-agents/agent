"""tj status: every open work item with its agents (live, idle or dead, from the hosts), worktree, PR and card."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_store
import lifecycle


def report(items: list[lifecycle.Item]) -> list[dict]:
    rows = []
    for item in items:
        worktree = item.worktree
        name = lifecycle.branch(worktree)
        rows.append({
            "item": item.ref,
            "title": (item.card or {}).get("title"),
            "card": (item.card or {}).get("state"),
            "worktree": worktree,
            "branch": name,
            "pr": lifecycle.pull_request(worktree, name),
            "agents": [{"session": agent.session["session"], "title": agent.session.get("title"),
                        "status": agent.status} for agent in item.agents],
        })
    return rows


def render(rows: list[dict]) -> str:
    if not rows:
        return "No open work items."
    lines = []
    for row in rows:
        pr = row["pr"]
        lines.append(f"{row['item']}  [{row['card'] or 'card unknown'}]  {row['title'] or ''}".rstrip())
        lines.append(f"  worktree  {row['worktree'] or '-'}" + (f"  ({row['branch']})" if row["branch"] else ""))
        if pr:
            lines.append(f"  pr        #{pr['number']} {pr['state'].lower()}, CI {pr['ci'] or 'none'}  {pr['url']}")
        for agent in row["agents"]:
            lines.append(f"  {agent['status']:<9} {agent['session']}" + (f"  '{agent['title']}'" if agent["title"] else ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tj status", description=__doc__)
    parser.add_argument("items", nargs="*", help="work item references; default: every open item")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)
    try:
        rows = report(lifecycle.open_items(args.items or None, cwd=Path.cwd()))
    except (lifecycle.LifecycleError, agent_store.AgentStoreError) as error:
        print(f"tj status: {error}", file=sys.stderr)
        return 1
    print(json.dumps(rows, indent=2) if args.json else render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
