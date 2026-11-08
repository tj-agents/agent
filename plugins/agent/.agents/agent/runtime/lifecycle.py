"""What the lifecycle commands share: tracker items, linked sessions, host liveness and pull requests."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from tj_agent.host import Host
from launch_contract import Liveness
import agent_cli
import agent_store

DONE = "Done"
COMMAND_TIMEOUT_SECONDS = 60


class LifecycleError(RuntimeError):
    pass


def command(arguments: list[str], cwd: Path | None = None) -> str:
    executable = shutil.which(arguments[0])
    if not executable:
        raise LifecycleError(f"{arguments[0]} is not on PATH")
    try:
        result = subprocess.run([executable, *arguments[1:]], cwd=cwd, capture_output=True, text=True,
                                encoding="utf-8", timeout=COMMAND_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise LifecycleError(f"{' '.join(arguments)} could not run: {error}") from error
    if result.returncode != 0:
        raise LifecycleError(f"{' '.join(arguments)} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def tj_item(*arguments: str, cwd: Path | None = None):
    return json.loads(command(["tj", "item", *arguments], cwd=cwd))


def liveness() -> dict[Host, dict[str, Liveness]]:
    return {host: agent_cli.host_launch(host).liveness() for host in Host}


@dataclass
class Agent:
    session: dict
    live: Liveness | None

    @property
    def host(self) -> Host:
        return Host(self.session["host"])

    @property
    def alive(self) -> bool:
        return self.live is not None and self.live.alive

    @property
    def status(self) -> str:
        if self.alive:
            return self.live.activity or "live"
        if self.session["state"] == "ended":
            return "ended"
        return "dead"


@dataclass
class Item:
    ref: str
    card: dict | None
    agents: list[Agent] = field(default_factory=list)

    @property
    def worktree(self) -> str | None:
        found = [agent.session["worktree"] for agent in self.agents if agent.session.get("worktree")]
        existing = [path for path in found if Path(path).is_dir()]
        return (existing or found or [None])[-1]


def open_items(refs: list[str] | None = None, cwd: Path | None = None) -> list[Item]:
    """The named items, or every item with linked sessions plus the tracker's items assigned to you,
    leaving out those the tracker says are done."""
    live = liveness()
    if refs is None:
        linked = [entry["item"]["ref"] for entry in agent_store.run("items")["items"]]
        try:
            mine = [card["ref"] for card in tj_item("mine", cwd=cwd)]
        except (LifecycleError, ValueError):
            mine = []
        refs = list(dict.fromkeys([*linked, *mine]))
    items = []
    for ref in refs:
        sessions = agent_store.run("sessions", "--item", ref)
        canonical = sessions["item"]["ref"]
        agents = [Agent(session, live[Host(session["host"])].get(session["native_id"]))
                  for session in sessions["sessions"]]
        item = Item(canonical, None, agents)
        try:
            item.card = tj_item("resolve", canonical, cwd=Path(item.worktree) if item.worktree else cwd)
        except (LifecycleError, ValueError):
            item.card = None
        if not item.card or item.card.get("state") != DONE:
            items.append(item)
    return items


def branch(worktree: str | None) -> str | None:
    if not worktree or not Path(worktree).is_dir():
        return None
    try:
        return command(["git", "-C", worktree, "branch", "--show-current"]).strip() or None
    except LifecycleError:
        return None


def pull_request(worktree: str | None, name: str | None) -> dict | None:
    if not worktree or not name:
        return None
    try:
        found = json.loads(command(["gh", "pr", "list", "--head", name, "--state", "all", "--limit", "1",
                                    "--json", "number,url,state,statusCheckRollup"], cwd=Path(worktree)))
    except (LifecycleError, ValueError):
        return None
    if not found:
        return None
    pr = found[0]
    return {"number": pr["number"], "url": pr["url"], "state": pr["state"],
            "ci": checks(pr.get("statusCheckRollup") or [])}


def checks(rollup: list[dict]) -> str | None:
    if not rollup:
        return None
    outcomes = {(check.get("conclusion") or check.get("state") or check.get("status") or "").upper()
                for check in rollup}
    if outcomes & {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}:
        return "failing"
    if outcomes - {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        return "pending"
    return "passing"


def repository_name(path: Path) -> str:
    top = Path(command(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"]).strip())
    return top.parent.name if top.name == ".git" else top.name.removesuffix(".git")

