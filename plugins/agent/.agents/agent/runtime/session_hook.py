"""Session hooks: SessionStart registers this session in agent-store, SessionEnd ends it, and each prompt
cancels a pending no-PR closeout of the `tj run` tab (`tj done --cancel`).

A session started by `tj run` binds to its launch (`TJ_RUN_LAUNCH_ID`) and inherits the launch's title,
parent, terminal, wrapper and work item. A Codex thread is also named after its title, so it is
addressable by name. Exit 0 always: bookkeeping never stops a session.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from tj_agent.host import Host
import agent_store
from tj_cli import process

LAUNCH_ENV = "TJ_RUN_LAUNCH_ID"
TITLE_ENV = "AGENT_CLI_TAB_TITLE"


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


def start(host: Host, session_id: str, cwd: str, environ=os.environ) -> dict:
    arguments = ["register", "--session", f"{host}:{session_id}", "--cwd", cwd]
    if root := worktree(cwd):
        arguments += ["--worktree", root]
    if launch := environ.get(LAUNCH_ENV):
        arguments += ["--launch-id", launch]
    else:
        arguments += terminal(environ)
        if title := environ.get(TITLE_ENV, "").strip():
            arguments += ["--title", title]
    session = agent_store.run(*arguments)["session"]
    if host is Host.CODEX and session.get("title"):
        import agent_cli

        agent_cli.host_launch(host).name_session(session_id, session["title"])
    return session


def end(host: Host, session_id: str) -> None:
    try:
        agent_store.run("end", "--session", f"{host}:{session_id}")
    except agent_store.AgentStoreError as error:
        if error.code not in ("not-found", "conflict"):
            raise


def prompt() -> None:
    tj = shutil.which("tj")
    if tj:
        process.run([tj, "done", "--cancel"], capture_output=True, timeout=8, check=False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", choices=("start", "prompt", "end"))
    parser.add_argument("--host", type=Host, choices=list(Host), required=True)
    args = parser.parse_args(argv)
    data = payload()
    if args.event == "prompt":
        try:
            prompt()
        except (OSError, subprocess.SubprocessError) as error:
            print(f"tj done --cancel: {error}", file=sys.stderr)
        return 0
    session_id = data.get("session_id") or data.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return 0
    try:
        if args.event == "start":
            start(args.host, session_id, data.get("cwd") or os.getcwd())
        else:
            end(args.host, session_id)
    except Exception as error:  # noqa: BLE001 - bookkeeping never stops a session
        print(f"agent-store {args.event}: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
