"""Record one native input's completion decision and execute its close transition."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def _session_close() -> Any:
    here = Path(__file__).resolve()
    locator = next(
        (
            root / ".agents/agent/runtime/package_root.py"
            for root in here.parents
            if (root / ".agents/agent/runtime/package_root.py").is_file()
        ),
        None,
    )
    if locator is None:
        raise RuntimeError("Agent peer runtime is unavailable")
    spec = importlib.util.spec_from_file_location("agent_close_package_root", locator)
    if spec is None or spec.loader is None:
        raise RuntimeError("Agent package locator is unavailable")
    package_root = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package_root)
    root = package_root.find_package_root(here)
    path = root / ".agents/agent/utility/peer/scripts/session_close.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("agent_close_session_close", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Agent peer runtime is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("assess")
    command.add_argument("--host", choices=("codex", "claude"), required=True)
    command.add_argument("--session-id", required=True)
    command.add_argument("--profile", required=True)
    command.add_argument("--decision", choices=("keep-open", "complete"), required=True)
    command.add_argument("--reason", required=True)
    command.add_argument("--transcript", type=Path)
    args = parser.parse_args(argv)
    try:
        if not args.reason.strip():
            raise ValueError("assessment needs a nonempty reason")
        shared = _session_close()
        shared.host_sessions(args.host)
        import close_lifecycle
        from host import Host

        result = close_lifecycle.assess(
            Host(args.host),
            args.session_id,
            Path(args.profile),
            args.decision,
            args.reason,
            shared,
            args.transcript,
        )
        print(json.dumps(result))
        return 2 if result["status"] == "unavailable" else 0
    except (OSError, RuntimeError, ValueError) as error:
        print(
            json.dumps({"status": "unavailable", "reason": str(error)}), file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
