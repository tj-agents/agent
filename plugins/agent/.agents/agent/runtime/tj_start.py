"""tj start: open a work item in its own worktree and an agent tab, and move its card to In Progress."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from host import Host
import agent_cli
import agent_store
import lifecycle

IN_PROGRESS = "In Progress"
WORKTREE_PATH = 'worktree-path = "{{ repo_path }}/../{{ repo }}.worktrees/{{ branch | sanitize }}"'
SLUG_WORDS = 6


def branch_name(card: dict) -> str:
    prefix = "Fix" if (card.get("kind") or "").casefold() == "bug" else "Feature"
    words = re.findall(r"[A-Za-z0-9]+", card.get("title") or "")[:SLUG_WORDS]
    slug = "".join(word[:1].upper() + word[1:] for word in words)
    native_id = card["ref"].rsplit(":", 1)[1]
    return f"{prefix}/{native_id}-{slug}" if slug else f"{prefix}/{native_id}"


def worktrunk_config(environ=os.environ) -> Path:
    if os.name == "nt":
        return Path(environ["APPDATA"]) / "worktrunk" / "config.toml"
    return Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "worktrunk" / "config.toml"


def seed_worktrunk(path: Path) -> None:
    """Give worktrunk the sibling `<repo>.worktrees/<Branch-Name>` layout unless its config chose one."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if re.search(r"^\s*worktree-path\s*=", text, re.MULTILINE):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WORKTREE_PATH + "\n" + text, encoding="utf-8")


def create_worktree(repository: Path, branch: str) -> str:
    seed_worktrunk(worktrunk_config())
    output = lifecycle.command(["git-wt", "switch", "--create", branch, "--no-cd", "--format", "json"],
                               cwd=repository)
    return json.loads(output.splitlines()[0])["path"]


def unique_title(base: str) -> str:
    title, number = base, 1
    while agent_store.run("sessions", "--title", title)["sessions"]:
        number += 1
        title = f"{base}-{number}"
    return title


def prompt(card: dict) -> str:
    return (f"Work on {card['ref']}: {card['title']} ({card.get('url') or 'no url'}). "
            f"Read it first with `tj item resolve {card['ref']} --body`, then follow the repository's workflow.")


def start(item_reference: str, host: Host, cwd: Path, lane: str | None = None, model: str | None = None) -> dict:
    card = lifecycle.tj_item("resolve", item_reference, cwd=cwd)
    linked = lifecycle.open_items([card["ref"]], cwd=cwd)
    existing = linked[0].worktree if linked else None
    worktree = existing if existing and Path(existing).is_dir() else create_worktree(cwd, branch_name(card))
    lifecycle.tj_item("progress", card["ref"], "--state", IN_PROGRESS, cwd=cwd)
    effort = None
    if lane:
        model, effort = agent_cli.resolve_lane_model(str(host), lane=lane)
    title = unique_title(f"{lifecycle.repository_name(cwd)}#{card['ref'].rsplit(':', 1)[1]}")
    adapter = agent_cli.host_launch(host)
    directory = Path(worktree)
    deliver = bool(effort) and host is Host.CODEX
    shown = agent_cli.launch(host, directory, title,
                             adapter.start_arguments(directory, title, prompt(card), model, effort),
                             item=card["ref"], want_session=deliver)
    if deliver:
        adapter.deliver(shown["session"]["native_id"], prompt(card), model, effort)
    return {"item": card["ref"], "worktree": worktree, "title": title, "host": str(host),
            "launch_id": shown["launch"]["launch_id"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tj start", description=__doc__)
    parser.add_argument("item", help="work item reference: 461, #461, a URL or provider:scope:id")
    hosts = parser.add_mutually_exclusive_group()
    hosts.add_argument("--claude", dest="host", action="store_const", const=Host.CLAUDE)
    hosts.add_argument("--codex", dest="host", action="store_const", const=Host.CODEX)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--lane", choices=("L1", "L2", "L3", "L4", "L5", "L6"))
    selection.add_argument("--model")
    args = parser.parse_args(argv)
    try:
        result = start(args.item, args.host or Host.CLAUDE, Path.cwd(), args.lane, args.model)
    except agent_cli.LaunchError as error:
        return agent_cli.report_launch_failure(error)
    except (lifecycle.LifecycleError, agent_store.AgentStoreError, KeyError, ValueError) as error:
        print(f"tj start: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
