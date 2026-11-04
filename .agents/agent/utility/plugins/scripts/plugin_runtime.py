"""Load shared process execution from this Agent package's shipped runtime."""

import importlib
import sys
from pathlib import Path

for root in Path(__file__).resolve().parents:
    runtime = root / ".agents/agent/runtime"
    markers = (
        ".git",
        ".codex-plugin/plugin.json",
        ".claude-plugin/plugin.json",
        "source.json",
    )
    if (runtime / "package_root.py").is_file() and any(
        (root / marker).exists() for marker in markers
    ):
        sys.path.insert(0, str(runtime))
        break
else:
    raise RuntimeError("The shipped Agent process runtime was not found")

process = importlib.import_module("tj_cli.process")

__all__ = ["process"]
