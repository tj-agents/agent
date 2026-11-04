"""Exercise plugin subprocess launches from a worker without a console."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock


def console_state() -> dict[str, bool | int]:
    state: dict[str, bool | int] = {"attached": False, "visible": False, "window": 0}
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.WinDLL("kernel32")
        user = ctypes.WinDLL("user32")
        kernel.GetConsoleWindow.restype = ctypes.c_void_p
        kernel.SetConsoleTitleW.argtypes = [ctypes.c_wchar_p]
        user.IsWindowVisible.argtypes = [ctypes.c_void_p]
        window = kernel.GetConsoleWindow()
        title = os.environ.get("TJ_AGENT_WINDOW_PROBE")
        if window and title:
            kernel.SetConsoleTitleW(title)
            time.sleep(0.05)
        state = {
            "attached": bool(window),
            "visible": bool(user.IsWindowVisible(window)),
            "window": int(window or 0),
        }
    return state


class ProbedError(Exception):
    def __init__(self, console: dict[str, bool | int]) -> None:
        self.console = console


def main() -> None:
    if sys.argv[1] == "grandchild":
        print(json.dumps(console_state()))
        return
    if sys.argv[1] == "child":
        state = console_state()
        descendant = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "grandchild"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=True,
        )
        child_state = json.loads(descendant.stdout)
        state["descendant_visible"] = child_state["visible"]
        state["descendant_window"] = child_state["window"]
        print(json.dumps(state))
        return

    scripts, operation, directory = sys.argv[1:]
    sys.path.insert(0, scripts)
    from plugin_runtime import process

    run = process.run

    def probe(arguments: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        result = run(
            [sys.executable, str(Path(__file__).resolve()), "child"], **options
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        raise ProbedError(json.loads(result.stdout))

    root = Path(directory)
    try:
        with mock.patch.object(process, "run", probe):
            adopt_plugins = importlib.import_module("adopt_plugins")
            config_conflicts = importlib.import_module("config_conflicts")
            machine_paths = importlib.import_module("machine_paths")
            repo_config = importlib.import_module("repo_config")
            update_marketplace_plugins = importlib.import_module(
                "update_marketplace_plugins"
            )

            match operation:
                case "selection":
                    repo_config.remote_owners(root)
                case "clone":
                    adopt_plugins.current_roster()
                case "snapshot":
                    update_marketplace_plugins.snapshot(root)
                case "tracked-settings":
                    (root / ".git").mkdir()
                    machine_paths.portable_tracked_text(root, ".codex/config.toml")
                case "conflicted-settings":
                    config = root / ".codex/config.toml"
                    config.parent.mkdir()
                    config.write_text(
                        '<<<<<<< ours\nmodel = "ours"\n=======\n'
                        'model = "theirs"\n>>>>>>> theirs\n',
                        encoding="utf-8",
                    )
                    config_conflicts.read_text(config)
                case _:
                    raise ValueError(operation)
    except ProbedError as result:
        print(
            json.dumps(
                {
                    "parent_console": console_state()["attached"],
                    "child_console": result.console["attached"],
                    "child_visible": result.console["visible"]
                    or result.console["descendant_visible"],
                    "console_windows": [
                        result.console["window"],
                        result.console["descendant_window"],
                    ],
                }
            )
        )
        return
    raise RuntimeError(f"{operation} did not launch a child")


if __name__ == "__main__":
    main()
