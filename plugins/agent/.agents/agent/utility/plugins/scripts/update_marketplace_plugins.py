#!/usr/bin/env python3
"""Generate Agent's offline roster and dependency snapshot from Marketplace."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import lock_io
from plugin_runtime import process

TARGET = Path(__file__).resolve().with_name("marketplace_plugins.lock.json")


def git(source: Path, *args: str) -> str:
    result = process.run(
        ["git", "-C", str(source), *args],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def snapshot(source: Path) -> str:
    revision = git(
        source,
        "log",
        "-1",
        "--format=%H",
        "HEAD",
        "--",
        "plugins.json",
        ".claude-plugin/marketplace.json",
    )
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Marketplace has no committed plugins.json")
    marketplace = json.loads(git(source, "show", f"{revision}:plugins.json"))
    if (
        not isinstance(marketplace, dict)
        or marketplace.get("name") != "tj-agents"
        or not isinstance(marketplace.get("plugins"), list)
    ):
        raise ValueError("Marketplace plugins document is invalid")
    catalog = json.loads(
        git(source, "show", f"{revision}:.claude-plugin/marketplace.json")
    )
    if (
        not isinstance(catalog, dict)
        or catalog.get("name") != "tj-agents"
        or not isinstance(catalog.get("plugins"), list)
    ):
        raise ValueError("Marketplace generated catalog is invalid")
    dependencies = {}
    for entry in catalog["plugins"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError("Marketplace generated catalog has an invalid plugin")
        name = entry["name"]
        if name in dependencies:
            raise ValueError(f"Marketplace generated catalog repeats {name}")
        dependencies[name] = lock_io.require_string_list(
            entry.get("dependencies", []), f"{name}.dependencies"
        )
    entries = []
    for entry in marketplace["plugins"]:
        if not isinstance(entry, dict) or entry.get("name") not in dependencies:
            raise ValueError(
                "Marketplace roster and generated catalog name different plugins"
            )
        entries.append(
            {
                **{
                    key: entry[key]
                    for key in ("name", "repository", "revision", "kind", "extends")
                    if key in entry
                },
                "dependencies": dependencies[entry["name"]],
            }
        )
    if len(entries) != len(dependencies) or len(
        {entry["name"] for entry in entries}
    ) != len(entries):
        raise ValueError(
            "Marketplace roster and generated catalog name different plugins"
        )
    marketplace["plugins"] = entries
    return (
        json.dumps(
            {
                "repository": "tj-agents/marketplace",
                "revision": revision,
                "marketplace": marketplace,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", required=True, type=Path, help="Marketplace checkout"
    )
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
    except (OSError, ValueError, lock_io.LockError, subprocess.TimeoutExpired) as error:
        print(f"Cannot generate Marketplace plugins: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
