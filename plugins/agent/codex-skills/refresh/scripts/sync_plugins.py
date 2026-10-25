#!/usr/bin/env python3
"""Run the refresh utility shipped in this machine package (Python 3.9+)."""

from pathlib import Path
import runpy

HERE = Path(__file__).resolve()


def target():
    for root in HERE.parents:
        markers = (root / ".git", root / ".codex-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json")
        candidate = root / ".agents" / "agent" / "runtime" / "reload_plugins.py"
        if candidate.is_file() and any(marker.exists() for marker in markers):
            return candidate
    raise RuntimeError("The packaged reload_plugins.py helper was not found")

if __name__ == "__main__":
    runpy.run_path(str(target()), run_name="__main__")
