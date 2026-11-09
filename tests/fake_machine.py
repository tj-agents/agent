"""A fake machine: Claude plugin installs, pins, setup and detached starts."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

CONVERGE = {"setup": {"script": "scripts/setup.py", "converge": True}}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def pin_plugin(claude_home: Path, plugin: str, pin: str) -> None:
    catalog = (
        claude_home / "plugins/marketplaces/tj-agents/.claude-plugin/marketplace.json"
    )
    entries = (
        json.loads(catalog.read_text(encoding="utf-8"))["plugins"]
        if catalog.is_file()
        else []
    )
    kept = [entry for entry in entries if entry["name"] != plugin]
    write_json(catalog, {"plugins": [*kept, {"name": plugin, "source": {"sha": pin}}]})


def install_plugin(
    claude_home: Path, plugin: str, revision: str, *, converge: bool = False
) -> Path:
    root = claude_home / "plugins/cache/tj-agents" / plugin / revision[:12]
    requires = CONVERGE if converge else {}
    write_json(
        root / "harness.json", {"plugin": f"tj-agents/{plugin}", "requires": requires}
    )
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts/setup.py").write_text("", encoding="utf-8")
    registry = claude_home / "plugins/installed_plugins.json"
    rows = (
        json.loads(registry.read_text(encoding="utf-8"))["plugins"]
        if registry.is_file()
        else {}
    )
    row = {"scope": "user", "gitCommitSha": revision, "installPath": str(root)}
    rows[f"{plugin}@tj-agents"] = [row]
    write_json(registry, {"plugins": rows})
    return root


class Started:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str], **_options: object) -> None:
        self.commands.append([str(part) for part in command])
