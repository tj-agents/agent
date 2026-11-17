"""tj status: every open work item with its agents (live, idle or dead, from the hosts), worktree, PR and card."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_store
import lifecycle
from launch_errors import LaunchError


def report(items: list[lifecycle.Item]) -> list[dict]:
    def row(item: lifecycle.Item) -> dict:
        worktree = item.worktree
        name = lifecycle.branch(worktree)
        try:
            result = lifecycle.tj_item("prs", item.ref, cwd=Path(worktree) if worktree else None)
            linked = []
            prs = result["prs"]
            if not isinstance(prs, list):
                raise ValueError("invalid linked pull request list")
            for pr in prs:
                if (not isinstance(pr, dict) or not isinstance(pr.get("state"), str)
                        or not isinstance(pr.get("url"), str)):
                    raise ValueError("invalid linked pull request")
                url = urlsplit(pr["url"])
                if url.scheme not in {"http", "https"} or not url.netloc or not url.path:
                    raise ValueError("invalid linked pull request URL")
                parts = url.path.strip("/").split("/")
                repository = "/".join(parts[:2]) if (
                    url.hostname == "github.com" and len(parts) >= 4 and parts[2] == "pull"
                ) else None
                linked.append({**pr, "repository": repository})
            error = None
        except (lifecycle.LifecycleError, ValueError, KeyError, TypeError) as failure:
            linked = None
            error = str(failure)
        return {
            "item": item.ref,
            "title": (item.card or {}).get("title"),
            "card": (item.card or {}).get("state"),
            "worktree": worktree,
            "branch": name,
            "pr": lifecycle.pull_request(worktree, name),
            "linked_prs": linked,
            "linked_prs_error": error,
            "agents": [{"session": agent.session["session"], "title": agent.session.get("title"),
                        "status": agent.status} for agent in item.agents],
        }
    with ThreadPoolExecutor(max_workers=lifecycle.LOOKUP_WORKERS) as pool:
        return list(pool.map(row, items))


def render(rows: list[dict]) -> str:
    if not rows:
        return "No open work items."
    lines = []
    for row in rows:
        pr = row["pr"]
        lines.append(f"{row['item']}  [{row['card'] or 'card unknown'}]  {row['title'] or ''}".rstrip())
        lines.append(f"  worktree  {row['worktree'] or '-'}" + (f"  ({row['branch']})" if row["branch"] else ""))
        if pr:
            lines.append(f"  branch pr #{pr['number']} {pr['state'].lower()}, CI {pr['ci'] or 'none'}  {pr['url']}")
        if row["linked_prs_error"] is not None:
            lines.append(f"  linked prs unavailable: {row['linked_prs_error']}")
        elif not row["linked_prs"]:
            lines.append("  linked prs none")
        else:
            for linked in row["linked_prs"]:
                repository = f" {linked['repository']}" if linked['repository'] else ""
                lines.append(f"  linked pr{repository} {linked['state']}  {linked['url']}")
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
    except (lifecycle.LifecycleError, agent_store.AgentStoreError, LaunchError) as error:
        print(f"tj status: {error}", file=sys.stderr)
        return 1
    print(json.dumps(rows, indent=2) if args.json else render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
