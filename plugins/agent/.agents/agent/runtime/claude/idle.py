"""Withdraw the owning tj run tab's pending `tj done` when Claude starts a tool."""

from __future__ import annotations

import subprocess
import sys

from tj_cli import process


def run(managed: bool) -> int:
    if not managed:
        return 0
    try:
        result = process.run(
            ["tj", "done", "--cancel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode:
            print(f"tj: done --cancel failed: {result.stderr.strip()}", file=sys.stderr)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"tj: done --cancel failed: {error}", file=sys.stderr)
    return 0
