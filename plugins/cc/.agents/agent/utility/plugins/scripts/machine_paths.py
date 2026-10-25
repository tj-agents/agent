#!/usr/bin/env python3
"""Find machine-specific absolute paths in committed host settings."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


COMMITTED_SETTINGS = (".claude/settings.json", ".codex/config.toml", ".codex/hooks.json")
MACHINE_PATH = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]"
    r"|(?<![A-Za-z0-9_.\\%$})])\\\\+[A-Za-z0-9_.$-]+\\"
    r"|(?<![A-Za-z0-9_.-])(?:/[A-Za-z])?/(?:home|Users)/[^/\\\s\"'*]+"
    r"|(?<![A-Za-z0-9_.-])/mnt/[A-Za-z]/"
)


def findings(text: str) -> list[tuple[int, str]]:
    return [
        (number, line.strip())
        for number, line in enumerate(text.splitlines(), start=1)
        if MACHINE_PATH.search(line)
    ]


def describe(relative: str, text: str) -> list[str]:
    return [f"{relative}:{number}: {line}" for number, line in findings(text)]


def scan(root: Path) -> list[str]:
    problems = []
    for relative in COMMITTED_SETTINGS:
        path = root / relative
        if path.is_file():
            problems += describe(relative, path.read_text(encoding="utf-8"))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        problems = scan(args.root)
    except (OSError, UnicodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    for problem in problems:
        print(problem)
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
