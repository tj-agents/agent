"""tj status: registered sessions, managed lifecycle receipts, work items and PRs."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_cli
import agent_store
import lifecycle
import work_item
from launch_errors import LaunchError
from tj_agent.host import Host


@dataclass(frozen=True, slots=True)
class ManagedStatus:
    state: str = "unverified"
    hold: bool | None = None
    receipt: dict[str, Any] | None = None
    details: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SessionStatus:
    session: str
    title: str
    host: str
    liveness: str
    launch_id: str | None
    parent: str | None
    relationship: str
    managed: ManagedStatus
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StatusReport:
    sessions: tuple[SessionStatus, ...] = ()
    items: list[dict[str, Any]] = field(default_factory=list)
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ItemLookup:
    item: lifecycle.Item
    error: str | None = None


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"receipt has invalid {field_name}")
    return value


def _receipt(launch_id: str, handle: str) -> ManagedStatus:
    try:
        document = json.loads(lifecycle.command(["tj", "receipt", "--launch", launch_id]))
        if not isinstance(document, dict) or type(document.get("schema")) is not int or document["schema"] != 1:
            raise ValueError("receipt has unsupported or missing schema")
        if document.get("identity") != {"launch_id": launch_id, "session_handle": handle}:
            raise ValueError("receipt identity does not match the current launch and session")
        if type(document.get("revision")) is not int or document["revision"] < 0:
            raise ValueError("receipt has invalid revision")
        if not isinstance(document.get("hold"), bool):
            raise ValueError("receipt has invalid hold")
        for name in ("withdrawals", "archive", "applied"):
            if not isinstance(document.get(name), list):
                raise ValueError(f"receipt has invalid {name}")
        state = document["state"]
        if not isinstance(state, dict):
            raise ValueError("receipt has invalid state")
        details = _state_details(state)
        if document["hold"] and state["kind"] in {"closing", "completed"}:
            raise ValueError("receipt has a hold during closeout")
        if state["kind"] == "closing" and state["revision"] > document["revision"]:
            raise ValueError("receipt closeout revision is newer than the record")
    except (lifecycle.LifecycleError, ValueError, KeyError, TypeError) as error:
        return ManagedStatus(error=f"{error}; inspect tj receipt --launch {launch_id}")
    else:
        return ManagedStatus(state["kind"], document["hold"], document, tuple(details))


def _state_details(state: dict[str, Any]) -> list[str]:
    details = []
    match state["kind"]:
        case "inactive":
            details.append("Waiting for native user input")
        case "active":
            _text(state["turn"], "turn")
            if state["intent"] is not None:
                if not isinstance(state["intent"], dict) or state["intent"].get("turn") != state["turn"]:
                    raise ValueError("receipt has invalid finish intent")
                details.append("Finish requested; waiting for the final response")
        case "waiting":
            _text(state["turn"], "turn")
            reason = state["reason"]
            if not isinstance(reason, dict) or reason.get("kind") not in {"user", "child", "ci", "approval"}:
                raise ValueError("receipt has invalid wait reason")
            details.append(f"Waiting for {reason['kind']}: {_text(reason['description'], 'wait description')}")
            if not isinstance(state["finish_requested"], bool):
                raise ValueError("receipt has invalid finish request")
            if state["finish_requested"]:
                details.append("Finish requested; pending work prevents closeout")
        case "attention":
            details.append(f"Needs attention: {_text(state['reason'], 'attention reason')}")
        case "ready" | "closing" | "completed":
            if state["kind"] == "closing" and (type(state["revision"]) is not int or state["revision"] < 1):
                raise ValueError("receipt has invalid closeout revision")
            if not isinstance(state.get("outcome"), dict):
                raise ValueError("receipt has no final result")
        case _:
            raise ValueError("receipt has unknown lifecycle state")
    if state["kind"] in {"waiting", "attention"} and state["report"] is not None:
        _text(state["report"], "report")
        details.append("Turn response recorded")
    outcome = state.get("outcome")
    if outcome is not None:
        if not isinstance(outcome, dict):
            raise ValueError("receipt has invalid final result")
        _text(outcome["turn"], "result turn")
        _text(outcome["report"], "final report")
        _text(outcome["receipt"]["event"], "result receipt")
        details.append("Final result recorded")
        for name in ("delivery", "tracker", "cleanup"):
            step = outcome["outbox"][name]
            match step["kind"]:
                case "pending":
                    details.append(f"{name}: pending")
                case "failed":
                    failure = step["failure"]
                    if failure["step"] != name:
                        raise ValueError("receipt has mismatched failure step")
                    details.append(f"{name}: failed — {_text(failure['description'], 'failure description')}")
                case "recorded":
                    _text(step["receipt"]["id"], f"{name} receipt")
                    details.append(f"{name}: recorded")
                case _:
                    raise ValueError("receipt has invalid closeout step")
            if state["kind"] == "completed" and step["kind"] != "recorded":
                raise ValueError("receipt claims completion without all closeout receipts")
    return details


def _session_status(session: dict, launches: dict, agent: lifecycle.Agent,
                    host_error: str | None) -> SessionStatus:
    handle = session["session"]
    launch_id = session.get("launch_id")
    launch = launches.get(launch_id)
    parent = None
    relationship = "unverified"
    issues = [host_error] if host_error else []
    if not launch_id:
        managed = ManagedStatus(error="No current managed launch is registered")
    elif not launch or launch.get("session") != handle or launch.get("host") != session["host"]:
        managed = ManagedStatus(error=f"Current launch {launch_id} has no matching session binding")
        launch = None
    else:
        parent = launch.get("parent")
        if "parent" not in launch or (parent is not None and (not isinstance(parent, str) or not parent.strip())):
            issues.append("Current launch has no valid parent relationship")
            parent = None
        else:
            relationship = "parented" if parent is not None else "independent"
        managed = _receipt(launch_id, handle)
    if not host_error and not agent.alive and managed.state != "completed":
        issues.append(f"Host is not running; inspect tj recover --session {handle} --dry-run")
    return SessionStatus(
        handle, (launch or {}).get("title") or session.get("title") or handle,
        session["host"], "unverified" if host_error else agent.status,
        launch_id, parent, relationship, managed, tuple(issues),
    )


def snapshot(refs: list[str] | None, cwd: Path) -> StatusReport:
    errors = []
    records: dict[str, dict] = {}
    item_sessions: dict[str, list[str]] = {}
    if refs:
        for ref in refs:
            try:
                data = agent_store.run("sessions", "--item", ref)
            except agent_store.AgentStoreError as error:
                errors.append(f"Sessions for {ref} unavailable: {error}")
            else:
                item_sessions[data["item"]["ref"]] = [row["session"] for row in data["sessions"]]
                records.update((row["session"], row) for row in data["sessions"])
    else:
        try:
            data = agent_store.run("sessions")
        except agent_store.AgentStoreError as error:
            errors.append(f"Sessions unavailable: {error}")
        else:
            records.update((row["session"], row) for row in data["sessions"])
        for record in records.values():
            for link in record.get("links", []):
                item_sessions.setdefault(link["item"]["ref"], []).append(record["session"])
    try:
        launches = {row["launch_id"]: row for row in agent_store.run("launches")["launches"]}
    except agent_store.AgentStoreError as error:
        launches = {}
        errors.append(f"Launch records unavailable: {error}")
    live = {}
    host_errors = {}
    for host in Host:
        if not any(row["host"] == host.value for row in records.values()):
            continue
        try:
            live[host.value] = agent_cli.host_launch(host).liveness(strict=True)
        except (LaunchError, OSError, RuntimeError) as error:
            host_errors[host.value] = f"Host liveness unavailable: {error}"
    agents = {handle: lifecycle.Agent(row, live.get(row["host"], {}).get(row["native_id"]))
              for handle, row in records.items()}

    def session_row(row: dict) -> SessionStatus:
        return _session_status(row, launches, agents[row["session"]], host_errors.get(row["host"]))

    with ThreadPoolExecutor(max_workers=lifecycle.LOOKUP_WORKERS) as pool:
        sessions = tuple(pool.map(session_row, records.values()))
    def resolve(ref: str) -> ItemLookup:
        handles = item_sessions[ref]
        item = lifecycle.Item(ref, None, [agents[handle] for handle in handles if handle in agents])
        try:
            item.card = lifecycle.tj_item("resolve", ref, cwd=Path(item.worktree) if item.worktree else cwd)
            if not isinstance(item.card, dict):
                raise ValueError("invalid work item card")
        except (lifecycle.LifecycleError, ValueError) as error:
            item.card = None
            return ItemLookup(item, f"Work item {ref} unavailable: {error}")
        else:
            return ItemLookup(item)

    with ThreadPoolExecutor(max_workers=lifecycle.LOOKUP_WORKERS) as pool:
        resolved = list(pool.map(resolve, item_sessions))
    items = []
    for lookup in resolved:
        if lookup.error:
            errors.append(lookup.error)
        item = lookup.item
        if item.card is None or item.card.get("state") != lifecycle.DONE:
            items.append(item)
    rows = report(items)
    by_handle = {session.session: session for session in sessions}
    for row in rows:
        for agent in row["agents"]:
            current = by_handle[agent["session"]]
            agent["status"] = current.liveness
            agent["title"] = current.title
    return StatusReport(sessions, rows, tuple(errors))


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


def render_items(rows: list[dict]) -> str:
    if not rows:
        return "No open linked or explicitly requested work items. Use tj item mine for assigned work."
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


def render(status: StatusReport) -> str:
    lines = []
    for session in status.sessions:
        lines.append(f"{session.title}  {session.session}")
        held = "unknown" if session.managed.hold is None else "active" if session.managed.hold else "off"
        lines.append(f"  host {session.host}: {session.liveness}; lifecycle: {session.managed.state}; hold: {held}")
        relationship = f"parent {session.parent}" if session.relationship == "parented" else session.relationship
        lines.append(f"  launch {session.launch_id or '-'}; {relationship}")
        lines.extend(f"  {detail}" for detail in session.managed.details)
        if session.managed.hold:
            lines.append("  Keep-open hold prevents automatic closure; tj hold --release in this session releases it")
        if session.managed.error:
            lines.append(f"  lifecycle unverified: {session.managed.error}")
        lines.extend(f"  issue: {issue}" for issue in session.issues)
    if not status.sessions:
        lines.append("No registered sessions found." if not status.errors else "No sessions could be listed; see lookup errors.")
    lines.append(render_items(status.items))
    lines.extend(f"Lookup error: {error}" for error in status.errors)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tj status", description=__doc__)
    parser.add_argument("items", nargs="*", help="filter sessions and work by item references; default: registered sessions and their linked open work; use tj item mine for assigned work")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)
    cwd = Path.cwd()
    try:
        refs = [work_item.reference(cwd, ref) for ref in args.items]
    except LaunchError as error:
        print(f"tj status: {error}", file=sys.stderr)
        return 1
    status = snapshot(refs or None, cwd)
    print(json.dumps(asdict(status), indent=2) if args.json else render(status))
    return int(bool(status.errors))


if __name__ == "__main__":
    sys.exit(main())
