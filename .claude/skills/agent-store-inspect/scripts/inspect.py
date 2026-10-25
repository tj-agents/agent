#!/usr/bin/env python3

import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
# Python adds this script directory ahead of the standard library. Its basename is
# inspect.py, so retain no shadowing entry before argparse imports dataclasses.
if sys.path and Path(sys.path[0]).resolve() == HERE:
    sys.path.pop(0)

import argparse
import importlib.util
import json


def _reader_path():
    for root in HERE.parents:
        markers = (root / ".git", root / ".codex-plugin" / "plugin.json", root / ".claude-plugin" / "plugin.json")
        path = root / ".agents" / "agent-store" / "runtime" / "reader.py"
        if path.is_file() and any(marker.exists() for marker in markers):
            return path
    raise RuntimeError("agent-store inspect cannot locate the shipped read-only store")


def _reader():
    path = _reader_path()
    spec = importlib.util.spec_from_file_location("agent_store_reader", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("agent-store inspect cannot load the shipped read-only store")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        reader = _reader()
        result = reader.ReadOnlyWorkItemStore(args.database).inspect(
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
