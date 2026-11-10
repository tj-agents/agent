"""A fake machine: plugin installs and pins, converged steps, detached starts."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import install
from codex import hook_trust

CONVERGE = {"setup": {"script": "scripts/setup.py", "converge": True}}
FILE_STEPS = ("launch-check runtime", "tj lifecycle commands", "shell profiles")


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


def environment(home: Path) -> dict[str, str]:
    return {
        "CLAUDE_CONFIG_DIR": str(home / "claude"),
        "CODEX_HOME": str(home / "codex"),
        "AGENT_STATE_DIRECTORY": str(home / "state"),
        "UV_TOOL_DIR": str(home / "uv-tools"),
        "TJ_BIN_DIR": str(home / "bin"),
        "APPDATA": str(home / "appdata"),
        "LOCALAPPDATA": str(home / "localappdata"),
    }


def write_tool_receipt(tools: Path, pins: Sequence[Mapping[str, str]]) -> None:
    rows = [
        f'{{ name = "{pin["package"]}", git = "https://github.com/{pin["repository"]}'
        f"?subdirectory={pin['subdirectory'].replace('/', '%2F')}"
        f'&rev={pin["revision"]}" }},'
        for pin in pins
    ]
    path = tools / pins[0]["package"] / "uv-receipt.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(["[tool]", "requirements = [", *rows, "]", ""])
    path.write_text(text, encoding="utf-8")


def machine_context(home: Path) -> Any:
    """A machine whose tools match their pins; call after `environment` is applied."""
    store = install.agent_store
    binary = b"agent-store"
    store.executable().parent.mkdir(parents=True, exist_ok=True)
    store.executable().write_bytes(binary)
    store.write_receipt(store.pinned_version(), binary)
    pins = json.loads(install.work_items_install.PINS.read_text(encoding="utf-8"))
    write_tool_receipt(Path(os.environ["UV_TOOL_DIR"]), [pins["tool"]])
    return install.Context(
        home=home / "home",
        environ=dict(os.environ),
        windows=os.name == "nt",
        documents=home / "Documents",
    )


def converge(context: Any, root: Path) -> None:
    context.root = root
    steps = [step for step in install.STEPS if step.name in FILE_STEPS]
    errors = io.StringIO()
    code = install.run_steps(steps, context, False, io.StringIO(), errors)
    assert code == 0, errors.getvalue()
    write_json(
        context.state_directory / hook_trust.RECEIPT, hook_trust.published_builds()
    )


def agent_root(root: Path, runtime: str) -> Path:
    for relative, text in (
        (".agents/agent/runtime/install.py", runtime),
        (".agents/agent/resources/profile.txt", "profile"),
        (".agents/agent/utility/plugins/scripts/sync.py", "sync"),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root
