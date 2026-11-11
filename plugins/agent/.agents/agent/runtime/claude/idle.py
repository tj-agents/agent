"""Pass Claude's native idle and background-work signal to its owning tj run wrapper."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping

from tj_cli import process


def background(data: Mapping[str, object]) -> str:
    tasks, wakeups = data.get("background_tasks"), data.get("session_crons")
    if not isinstance(tasks, list) or not isinstance(wakeups, list):
        return "unknown"
    if any(not isinstance(entry, dict) for entry in [*tasks, *wakeups]):
        return "unknown"
    return str(len(tasks) + len(wakeups))


def run(data: Mapping[str, object], managed: bool) -> int:
    if not managed:
        return 0
    event = data.get("hook_event_name")
    if event == "Stop":
        arguments = ["idle", "--background", background(data)]
    elif event == "PreToolUse":
        arguments = ["done", "--cancel"]
    else:
        return 0
    try:
        result = process.run(
            ["tj", *arguments], capture_output=True, text=True, timeout=5, check=False
        )
        if result.returncode:
            print(f"tj: idle signal failed: {result.stderr.strip()}", file=sys.stderr)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"tj: idle signal failed: {error}", file=sys.stderr)
    return 0
