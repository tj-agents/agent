"""Claude executable selection, session environment and native launch settings."""

import os
from pathlib import Path
import shutil
import sys

from launch_errors import LaunchError
from native_executable import is_script

IS_WINDOWS = os.name == "nt"
KEEP_TAB_TITLE = {"CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"}
SESSION_ENVIRONMENT = (
    "NO_COLOR",
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PID",
)
RELAUNCH_ENVIRONMENT = (
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID",
    "CLAUDE_CODE_ENTRYPOINT",
)


def resolve_executable(home=None, which=shutil.which):
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


def open_tab(
    cli,
    working_directory,
    title,
    arguments,
    status_file=None,
    require_session_start=False,
    clear=(),
    force=None,
):
    claude = resolve_executable()

    cli.launch_tab(
        working_directory,
        sys.executable,
        title,
        arguments=cli.bound_terminal_arguments(
            "claude",
            working_directory,
            claude,
            list(arguments),
            status_file,
            require_session_start,
        ),
        clear=clear,
        force={
            "FORCE_COLOR": "1",
            "TERM": "xterm-256color",
            **KEEP_TAB_TITLE,
            **(force or {}),
        },
    )
