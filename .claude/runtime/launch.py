"""Claude executable selection, launch arguments and session liveness."""

import json
import os
from pathlib import Path
import shutil
import subprocess
from collections.abc import Callable

from launch_contract import Liveness
from launch_errors import LaunchError
from native_executable import is_script
from tj_agent.host import Host
from tj_cli import process

HOST = Host.CLAUDE
IS_WINDOWS = os.name == "nt"
SESSION_ENVIRONMENT: tuple[str, ...] = (
    "NO_COLOR",
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    HOST.session_variable,
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PID",
)
TAB_CLEARED: tuple[str, ...] = ()
TAB_FORCED = {"FORCE_COLOR": "1", "TERM": "xterm-256color"}


def resolve_executable(home=None, which: Callable[[str], str | None] = shutil.which) -> str:
    home = Path(home) if home else Path.home()
    name = "claude.exe" if IS_WINDOWS else "claude"
    local = home / ".local" / "bin" / name
    found = which(name)
    for candidate in (str(local), found):
        if (
            candidate
            and os.path.isfile(candidate)
            and (IS_WINDOWS or not is_script(os.path.realpath(candidate)))
        ):
            return candidate

    raise LaunchError(
        f"The native Claude Code executable was not found. Looked for {local} and a native {name} on PATH."
    )


def executable() -> str:
    return resolve_executable()


def start_arguments(directory, title, prompt, model, effort):
    arguments = ["--name", title]
    if model:
        arguments += ["--model", model]
    if effort:
        arguments += ["--effort", effort]
    return [*arguments, prompt]


def resume_arguments(directory, title, session_id):
    return ["--resume", session_id, "--name", title]


def liveness(run=process.run) -> dict[str, Liveness]:
    """Every Claude session the host lists, by session id: alive while it reports a pid."""
    try:
        result = run([executable(), "agents", "--json", "--all"], capture_output=True, text=True,
                     encoding="utf-8", timeout=30, check=False)
        rows = json.loads(result.stdout) if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError, LaunchError):
        return {}
    return {
        row["sessionId"]: Liveness(
            alive="pid" in row,
            activity=row.get("status") or row.get("state"),
            background=row.get("id") if row.get("kind") == "background" else None,
        )
        for row in rows if isinstance(row, dict) and isinstance(row.get("sessionId"), str)
    }


def respawn(background, run=process.run):
    result = run([executable(), "respawn", background], capture_output=True, text=True, encoding="utf-8",
                 timeout=60, check=False)
    if result.returncode != 0:
        raise LaunchError(f"claude respawn {background} failed: {result.stderr.strip() or result.stdout.strip()}")


def name_session(session_id, title):
    return None


def deliver(session_id, prompt, model, effort):
    raise LaunchError("Claude takes its first prompt on the command line")
