"""A fake machine: plugin installs and pins, and converged steps."""

from __future__ import annotations

import io
import json
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import install
import jobs
from codex import catalog as codex_catalog
from codex import hook_trust

CONVERGE = {"setup": {"script": "scripts/setup.py", "converge": True}}
FILE_STEPS = ("launch-check runtime", "tj lifecycle commands", "shell profiles")


MARKETPLACE_REVISION = "f" * 40


def recorded_failures(state: Path, job: jobs.Job = jobs.Job.UPDATE) -> list[str]:
    status = json.loads(jobs.status_path(state, job).read_text(encoding="utf-8"))
    return list(status["failures"])


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@dataclass
class CodexPlugins:
    home: Path
    versions: Mapping[str, str]
    hidden: set[str] = field(default_factory=set)

    def listing(self) -> str:
        return json.dumps(
            {
                "installed": [
                    {
                        "pluginId": identity,
                        "marketplaceName": identity.rpartition("@")[2],
                        "version": version,
                        "source": {"source": "git-subdir", "sha": "a" * 40},
                        "installed": True,
                    }
                    for identity, version in self.versions.items()
                    if identity not in self.hidden
                ]
            }
        )

    def install(self, identity: str) -> None:
        self.hidden.discard(identity)
        plugin, _, marketplace = identity.rpartition("@")
        version = self.versions[identity]
        directory = self.home / "plugins/cache" / marketplace / plugin
        for previous in list(directory.iterdir()) if directory.is_dir() else []:
            if previous.is_dir() and previous.name != version:
                moved = self.home / "superseded" / marketplace / plugin / previous.name
                moved.parent.mkdir(parents=True, exist_ok=True)
                previous.rename(moved)
        write_json(
            directory / version / ".codex-plugin/plugin.json",
            {"name": plugin, "version": version},
        )
        write_json(
            self.home
            / ".tmp/marketplaces"
            / marketplace
            / ".agents/plugins/marketplace.json",
            {
                "plugins": [
                    {
                        "name": name.rpartition("@")[0],
                        "source": {"source": "git-subdir", "sha": "a" * 40},
                    }
                    for name in self.versions
                    if name.rpartition("@")[2] == marketplace
                ]
            },
        )


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


def record_codex_plugin(home: Path, plugin: str, pin: str, version: str) -> None:
    source = {"source": "git-subdir", "sha": pin}
    path = home / ".tmp/marketplaces/tj-agents/.agents/plugins/marketplace.json"
    entries = (
        json.loads(path.read_text(encoding="utf-8"))["plugins"]
        if path.is_file()
        else []
    )
    kept = [entry for entry in entries if entry["name"] != plugin]
    write_json(path, {"plugins": [*kept, {"name": plugin, "source": source}]})
    root = home / "plugins/cache/tj-agents" / plugin / version
    write_json(root / ".codex-plugin/plugin.json", {"name": plugin, "version": version})
    codex_catalog.remember(
        [{"pluginId": f"{plugin}@tj-agents", "source": source, "version": version}],
        verified_install=True,
    )


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


CLAUDE_REGISTRY = threading.Lock()


def update_claude_plugin(claude_home: Path, command: Sequence[str]) -> None:
    plugin, scope = command[3], command[command.index("--scope") + 1]
    name, _, marketplace = plugin.rpartition("@")
    catalog = (
        claude_home
        / "plugins/marketplaces"
        / marketplace
        / ".claude-plugin/marketplace.json"
    )
    with CLAUDE_REGISTRY:
        pins = (
            {
                entry["name"]: entry["source"]["sha"]
                for entry in json.loads(catalog.read_text(encoding="utf-8"))["plugins"]
            }
            if catalog.is_file()
            else {}
        )
        registry = claude_home / "plugins/installed_plugins.json"
        if name not in pins or not registry.is_file():
            return
        document = json.loads(registry.read_text(encoding="utf-8"))
        root = claude_home / "plugins/cache" / marketplace / name / pins[name][:12]
        root.mkdir(parents=True, exist_ok=True)
        for row in document["plugins"].get(plugin, []):
            if row["scope"] == scope:
                row.update(gitCommitSha=pins[name], installPath=str(root))
        write_json(registry, document)


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
        context.state_directory / hook_trust.RECEIPT, hook_trust.published_hooks()
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
