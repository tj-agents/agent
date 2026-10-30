#!/usr/bin/env python3

import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
# Python adds this script directory ahead of the standard library. Its basename is
# inspect.py, so retain no shadowing entry before argparse imports dataclasses.
if sys.path and Path(sys.path[0]).resolve() == HERE:
    sys.path.pop(0)

import argparse
import json


def _store_root():
    for root in HERE.parents:
        markers = (root / ".git", root / ".codex-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json")
        runtime = root / ".agents" / "agent-store" / "runtime"
        if (runtime / "tj_agent_store" / "__init__.py").is_file() and any(marker.exists() for marker in markers):
            return runtime
    raise RuntimeError("agent-store inspect cannot locate the shipped read-only store")


def _agent_store():
    sys.path.insert(0, str(_store_root()))
    import tj_agent_store

    return tj_agent_store


def arguments(argv=None):
    parser = argparse.ArgumentParser(description="Inspect recorded Agent Store state.")
    parser.add_argument("--database", type=Path)
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--execution-key")
    selector.add_argument("--repository")
    return parser.parse_args(argv)


def main(argv=None):
    args = arguments(argv)
    try:
        store = _agent_store()
        result = store.ReadOnlyWorkItemStore(args.database).inspect(
            execution_key=args.execution_key,
            repository=args.repository,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
