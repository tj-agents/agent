#!/usr/bin/env python3
"""Dispatch a prepared handoff to the required native host launcher."""

from tj_agent.hosts import Host
from tj_agent.hosts.claude import handoff as claude_handoff
from tj_agent.hosts.codex import handoff as codex_handoff

HANDLERS = {Host.CLAUDE: claude_handoff, Host.CODEX: codex_handoff}

import argparse
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Hand prepared work to an independent Claude or Codex session."
    )
    host_type = Host
    parser.add_argument(
        "--host", type=host_type, choices=list(host_type), required=True
    )
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        parser.error("separate host selection from launcher arguments with --")
    separator = argv.index("--")
    args = parser.parse_args(argv[:separator])
    remaining = argv[separator + 1 :]
    return HANDLERS[args.host].main(remaining)


if __name__ == "__main__":
    sys.exit(main())
