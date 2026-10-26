"""Run one packaged detached callback with its exact Agent state context."""

import argparse
import os
from pathlib import Path
import runpy
import sys


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-directory", required=True, type=Path)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--claude-config-dir", type=Path)
    parser.add_argument("script", type=Path)
    parser.add_argument("script_arguments", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def main(argv=None):
    values = arguments(argv)
    state = values.state_directory.resolve()
    if not state.is_absolute() or not values.script.is_file():
        raise ValueError("detached callback context is unavailable")
    os.environ["AGENT_STATE_DIRECTORY"] = str(state)
    for key, value in (("CODEX_HOME", values.codex_home), ("CLAUDE_CONFIG_DIR", values.claude_config_dir)):
        if value is not None:
            os.environ[key] = str(value.resolve())
    script = values.script.resolve()
    sys.path.insert(0, str(script.parent))
    sys.argv = [str(script), *values.script_arguments]
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
