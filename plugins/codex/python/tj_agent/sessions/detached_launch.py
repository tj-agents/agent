"""Run one packaged detached callback with its exact Agent state context."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import sys
from pathlib import Path


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-directory", required=True, type=Path)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--claude-config-dir", type=Path)
    parser.add_argument("script", type=Path)
    parser.add_argument("script_arguments", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    values = arguments(argv)
    state = values.state_directory.resolve()
    if not state.is_absolute() or not values.script.is_file():
        raise ValueError("detached callback context is unavailable")
    os.environ["AGENT_STATE_DIRECTORY"] = str(state)
    for key, value in (
        ("CODEX_HOME", values.codex_home),
        ("CLAUDE_CONFIG_DIR", values.claude_config_dir),
    ):
        if value is not None:
            os.environ[key] = str(value.resolve())
    script = values.script.resolve()
    boundary = Path(__file__).resolve().parent
    receipt = json.loads(
        (boundary / ".tj-agents-observer.json").read_text(encoding="utf-8")
    )
    if receipt.get("kind") != "tj-agents-agent-observer" or receipt.get("version") != 1:
        raise ValueError("detached callback receipt is unavailable")
    expected = receipt.get("files")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("detached callback receipt is malformed")
    actual = {}
    for path in sorted(boundary.rglob("*")):
        if path.is_symlink() or bool(
            getattr(path.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("detached callback contains a symbolic link")
        if path.is_file() and path.name != ".tj-agents-observer.json":
            actual[path.relative_to(boundary).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    if actual != expected or not script.is_relative_to(boundary):
        raise ValueError("detached callback closure changed")
    sys.path.insert(0, str(boundary / "python"))
    sys.argv = [str(script), *values.script_arguments]
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
