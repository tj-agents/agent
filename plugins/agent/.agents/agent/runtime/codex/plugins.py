"""Codex's native plugin inventory contract (Python 3.9+)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import agent_cli
from codex import hook_trust
from host import Host
import required_plugins

HOST = Host.CODEX
VALUE_OPTIONS = frozenset({
    "-C", "--cd", "-c", "--config", "-m", "--model", "-p", "--profile",
    "-s", "--sandbox", "-a", "--ask-for-approval", "--add-dir", "--enable", "--disable",
    "--remote", "--remote-auth-token-env", "-i", "--image", "--local-provider",
})
CONFIGURATION_OPTIONS = frozenset({"-c", "--config", "-p", "--profile", "--enable", "--disable"})
FLAG_OPTIONS = frozenset({
    "--no-alt-screen", "--search", "--full-auto", "--dangerously-bypass-approvals-and-sandbox",
    "--no-daemon", "--strict-config", "--oss", "--approve-for-me",
    "--dangerously-bypass-hook-trust", "--worktree",
})
DIRECTORY_OPTIONS = frozenset({"-C", "--cd"})
INFORMATION_OPTIONS = frozenset({"--help", "-h", "--version", "-V"})
MAINTENANCE_COMMANDS = frozenset({
    "plugin", "login", "logout", "doctor", "help", "completion", "update", "mcp", "features",
})
INVENTORY_CONFIG_KEYS = frozenset({
    "model", "model_provider", "model_reasoning_effort", "model_reasoning_summary",
    "model_verbosity", "service_tier", "personality", "tui.terminal_title",
})
NODE_SHIM_BYPASSED = True


def resolve_executable() -> str:
    return agent_cli.resolve_codex_executable()[0]


def installations(project: Path, entries: list) -> list:
    return entries


def location(entry: dict, project: Path) -> tuple[str, Path] | None:
    owner = entry.get("projectPath")
    if owner and entry.get("scope") not in ("user", "managed"):
        if not project.resolve().is_relative_to(Path(owner).resolve()):
            return None
    name, marketplace, version = (entry.get(key) for key in ("name", "marketplaceName", "version"))
    if not all(isinstance(value, str) and value for value in (name, marketplace, version)):
        return None
    identity = f"{name}@{marketplace}"
    if (not required_plugins.IDENTITY.fullmatch(identity) or version in (".", "..")
            or "/" in version or "\\" in version):
        return None
    return identity, HOST.configuration_root / "plugins" / "cache" / marketplace / name / version


def require_inventory_options(options: tuple[str, ...]) -> None:
    for option, value in zip(options[::2], options[1::2]):
        key = value.partition("=")[0].strip().strip("\"'")
        if option not in ("-c", "--config") or key not in INVENTORY_CONFIG_KEYS:
            raise required_plugins.RequiredPluginsError(
                f"Cannot verify required Codex plugins with launch override {option}: native plugin "
                "inventory does not apply session activation overrides. Use the persisted repository "
                "configuration for plugin, profile and feature settings, then relaunch. "
                "Model, reasoning and terminal-title overrides remain available."
            )


def inventory_marketplaces(required: tuple[str, ...]) -> list[str | None]:
    return sorted({identity.rsplit("@", 1)[1] for identity in required})


def inventory_entries(data: object) -> object:
    return data.get("installed") if isinstance(data, dict) else data


def ensure_hooks(project: Path, executable: str | None = None, *, running_session: bool = False,
                 required_roots: Mapping[str, Path] | None = None) -> str | None:
    required = required_plugins.selected(required_plugins.project_root(project))
    if not required:
        return None
    binary = executable or resolve_executable()
    expected = {}
    roots = required_plugins.check(project, HOST, binary) if required_roots is None else required_roots
    for identity, root in (roots or {}).items():
        definitions = hook_trust.required_definitions(root, identity)
        if definitions:
            expected[identity] = definitions
    hook_trust.apply_trust(binary, project.resolve(), required=expected)
    if running_session:
        try:
            hook_trust.reload_running(binary, project.resolve(), required=expected)
        except (OSError, RuntimeError) as error:
            return str(error)
    return None
