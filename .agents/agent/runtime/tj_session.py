import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import agent_store
from tj_agent import worktrees


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record session worktree ownership")
    commands = parser.add_subparsers(dest="command", required=True)
    claim = commands.add_parser("claim-worktree")
    claim.add_argument("--path", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        owner = worktrees.current_owner(os.environ)
        ownership = worktrees.claim(arguments.path, owner) if owner else None
    except (OSError, worktrees.OwnershipError, agent_store.AgentStoreError) as error:
        print(f"tj session claim-worktree: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"ownership": ownership.document() if ownership else None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
