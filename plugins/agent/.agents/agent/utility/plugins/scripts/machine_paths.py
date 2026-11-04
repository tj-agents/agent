#!/usr/bin/env python3
"""Find machine-specific absolute paths in committed host settings."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import lock_io

COMMITTED_SETTINGS = (
    ".claude/settings.json",
    ".codex/config.toml",
    ".codex/hooks.json",
)
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


def portable_tracked_text(root: Path, relative: str) -> str | None:
    if not (root / ".git").exists():
        return None

    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "--literal-pathspecs", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )

    def checked(*arguments: str) -> str:
        result = git(*arguments)
        if result.returncode:
            raise lock_io.LockError(
                f"Cannot inspect tracked {relative}: {result.stderr.strip()}"
            )
        return result.stdout

    indexed = checked("ls-files", "--stage", "--", relative)
    snapshots = {
        f"index:{stage}": checked("cat-file", "blob", oid)
        for entry in indexed.splitlines()
        for _, oid, stage in [entry.split("\t", 1)[0].split()]
    }
    head = git("rev-parse", "--verify", "--quiet", "HEAD")
    if head.returncode not in {0, 1}:
        raise lock_io.LockError(
            f"Cannot inspect tracked {relative}: {head.stderr.strip()}"
        )
    if head.returncode == 0:
        committed = checked("ls-tree", "HEAD", "--", relative)
        if committed:
            oid = committed.split("\t", 1)[0].split()[2]
            snapshots["HEAD"] = checked("cat-file", "blob", oid)
    problems = [
        problem
        for revision, text in snapshots.items()
        for problem in describe(f"{relative} ({revision})", text)
    ]
    if problems:
        raise lock_io.LockError(
            "Committed host settings contain machine-specific paths:\n"
            + "\n".join(problems)
        )
    if "HEAD" in snapshots and (
        "index:0" in snapshots or {"index:1", "index:2", "index:3"} <= snapshots.keys()
    ):
        return snapshots["HEAD"]
    return None


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
