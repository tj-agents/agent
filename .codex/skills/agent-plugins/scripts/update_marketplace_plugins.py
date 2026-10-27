#!/usr/bin/env python3
"""Generate Machine's offline plugin snapshot from a Marketplace checkout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


TARGET = Path(__file__).resolve().with_name("marketplace_plugins.lock.json")


def git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args], capture_output=True, check=False,
        text=True, encoding="utf-8", timeout=30,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def snapshot(source: Path) -> str:
    revision = git(source, "log", "-1", "--format=%H", "HEAD", "--", "plugins.json")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Marketplace has no committed plugins.json")
    marketplace = json.loads(git(source, "show", f"{revision}:plugins.json"))
    if marketplace.get("name") != "tj-agents" or not isinstance(marketplace.get("plugins"), list):
        raise ValueError("Marketplace plugins document is invalid")
    return json.dumps({
        "repository": "tj-agents/marketplace", "revision": revision, "marketplace": marketplace,
    }, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Marketplace checkout")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        expected = snapshot(args.source.resolve())
        if args.check:
            if not TARGET.is_file() or TARGET.read_text(encoding="utf-8") != expected:
                print(f"Stale marketplace snapshot: {TARGET}", file=sys.stderr)
                return 1
        else:
            TARGET.write_text(expected, encoding="utf-8", newline="\n")
    except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        print(f"Cannot generate Marketplace plugins: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
