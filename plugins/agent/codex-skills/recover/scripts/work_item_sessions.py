#!/usr/bin/env python3

import importlib.util
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent


def _script():
    for root in HERE.parents:
        markers = (root / ".git", root / ".codex-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json")
        candidate = root / ".agents" / "agent" / "runtime" / "work_item_sessions.py"
        if candidate.is_file() and any(marker.exists() for marker in markers):
            return candidate
    return None


def main(argv=None):
    path = _script()
    if path is None:
        raise RuntimeError("The packaged work_item_sessions.py helper was not found relative to Agent Recover.")
    spec = importlib.util.spec_from_file_location("recover_agents_work_item_sessions", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"The shared work_item_sessions.py helper could not be loaded from {path}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(argv)


if __name__ == "__main__":
    sys.exit(main())
