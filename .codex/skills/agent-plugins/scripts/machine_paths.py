#!/usr/bin/env python3
"""Find machine-specific absolute paths in committed host settings."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import lock_io
from plugin_runtime import process

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


def tracked_snapshots(root: Path, relative: str) -> dict[str, str]:
    revisions = {f"index:{stage}": f":{stage}:{relative}" for stage in range(4)}
    revisions["HEAD"] = f"HEAD:{relative}"
    result: subprocess.CompletedProcess[bytes] = process.run(
        ["git", "cat-file", "--batch"],
        cwd=root,
        input="".join(f"{name}\n" for name in revisions.values()).encode("utf-8"),
        capture_output=True,
        timeout=10,
    )
    if result.returncode:
        raise lock_io.LockError(
            f"Cannot inspect tracked {relative}: "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    snapshots = {}
    position = 0
    for label, name in revisions.items():
        end = result.stdout.index(b"\n", position)
        header = result.stdout[position:end].decode("utf-8")
        position = end + 1
        if header == f"{name} missing":
            continue
        _, kind, size = header.rsplit(" ", 2)
        if kind != "blob":
            raise lock_io.LockError(f"Tracked {relative} is a {kind}, not a file")
        content = result.stdout[position : position + int(size)]
        position += int(size) + 1
        snapshots[label] = (
            content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        )
    return snapshots


def portable_tracked_text(root: Path, relative: str) -> str | None:
    if not (root / ".git").exists():
        return None
    snapshots = tracked_snapshots(root, relative)
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
