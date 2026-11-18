"""Session hooks: SessionStart registers this session in agent-store, and SessionEnd ends it for an
explicit user exit and otherwise leaves it active for `tj recover`.

A session owned by a managed launch, as `tj launch` reports it, binds to that launch and inherits its title,
parent, terminal, wrapper and work item; a session that only inherited the wrapper environment registers
unbound. When `tj launch` cannot answer, the session is left unregistered and the failure reported, never
registered unbound beside the launch it may own. A Codex thread is also named after its title, so it is addressable by name.
Bookkeeping errors remain non-blocking; the managed prompt hook enforces admission before native work.
Claude's outer hook budget includes ownership reads, the 90-second resume admission and linked-worktree
checks: their individual subprocess limits total 275 seconds, below the 300-second native hook limit.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from tj_agent.host import Host
from tj_agent import worktrees
from codex import launch as codex_launch
from codex import processes as codex_processes
import agent_cli
import agent_store
import owned_launch
import session_resume
from tj_cli import process

LAUNCH_ENV = "TJ_RUN_LAUNCH_ID"
TITLE_ENV = "AGENT_CLI_TAB_TITLE"
WRAPPER_PID_ENV = "TJ_RUN_PID"
WRAPPER_STARTED_AT_ENV = "TJ_RUN_STARTED_AT"
EXPLICIT_EXIT_REASONS = frozenset({"clear", "resume", "logout", "prompt_input_exit"})


def payload() -> dict:
    try:
        value = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def worktree(cwd: str) -> str | None:
    try:
        result = process.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def terminal(environ) -> list[str]:
    if environ.get("TMUX_PANE"):
        return ["--terminal-kind", "tmux", "--terminal-id", environ["TMUX_PANE"]]
    if os.name == "nt" and environ.get("WT_SESSION"):
        return ["--terminal-kind", "windows-terminal", "--terminal-id", environ["WT_SESSION"]]
    return []


def title_arguments(environ: Mapping[str, str]) -> list[str]:
    if title := environ.get(TITLE_ENV, "").strip():
        return ["--title", title]
    return []


def register(host: Host, session_id: str, cwd: str, root: str | None, arguments: list[str]) -> dict:
    base = ["register", "--session", f"{host}:{session_id}", "--cwd", cwd]
    if root:
        base += ["--worktree", root]
    return agent_store.run(*base, *arguments)["session"]


def start(host: Host, session_id: str, cwd: str, environ: Mapping[str, str]) -> dict:
    root = worktree(cwd)
    adapter = agent_cli.host_launch(host)
    session = codex_launch.registered_session(session_id) if host is Host.CODEX else None
    inherited = environ.get(LAUNCH_ENV)
    cleared: tuple[str, ...] = ()
    launch = owned_launch.for_session(f"{host}:{session_id}") if session is None and inherited else None
    environ = {name: value for name, value in environ.items() if name != LAUNCH_ENV}
    if session is None and inherited and launch is None:
        cleared = tuple(
            name for name in (*environ, LAUNCH_ENV)
            if name.startswith("TJ_RUN_") or name in {TITLE_ENV, "WT_SESSION", "TMUX_PANE"}
        )
        environ = {
            name: value for name, value in environ.items()
            if not name.startswith("TJ_RUN_") and name not in {TITLE_ENV, "WT_SESSION", "TMUX_PANE"}
        }
    if session is None:
        if launch:
            environ[LAUNCH_ENV] = launch
            if host is Host.CODEX:
                codex_processes.record(session_id, launch)
            session = (
                session_resume.bind_started(f"{host}:{session_id}", launch)
                if host is Host.CLAUDE else None
            )
            if session is None:
                session = register(host, session_id, cwd, root, ["--launch-id", launch])
        else:
            session = register(host, session_id, cwd, root, [*terminal(environ), *title_arguments(environ)])
    else:
        environ = {host.session_variable: session_id, LAUNCH_ENV: session["launch_id"]}
    try:
        if host is Host.CODEX and session.get("title"):
            adapter.name_session(session_id, session["title"])
    finally:
        adapter.record_session_environment(session_id, environ, cleared=cleared)
        if root:
            worktrees.claim_started_worktree(host, session_id, Path(root), environ)
    return session


def end(host: Host, session_id: str, reason: str | None) -> None:
    if reason not in EXPLICIT_EXIT_REASONS:
        return
    try:
        agent_store.run("end", "--session", f"{host}:{session_id}", "--reason", reason)
    except agent_store.AgentStoreError as error:
        if error.code not in ("not-found", "conflict"):
            raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", choices=("start", "end"))
    parser.add_argument("--host", type=Host, choices=list(Host), required=True)
    args = parser.parse_args(argv)
    data = payload()
    session_id = data.get("session_id") or data.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return 0
    try:
        if args.event == "start":
            start(args.host, session_id, data.get("cwd") or os.getcwd(), os.environ)
        else:
            reason = data.get("reason")
            end(args.host, session_id, reason if isinstance(reason, str) else None)
    except Exception as error:  # noqa: BLE001 - bookkeeping never stops a session
        print(f"agent-store {args.event}: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
