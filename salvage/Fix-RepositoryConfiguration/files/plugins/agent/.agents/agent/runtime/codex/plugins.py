"""Codex's native plugin inventory contract (Python 3.9+)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import agent_cli
import installer_boundary
import required_plugins
import standards
from codex import catalog, hook_trust
from tj_agent.host import Host

HOST = Host.CODEX
VALUE_OPTIONS = frozenset(
    {
        "-C",
        "--cd",
        "-c",
        "--config",
        "-m",
        "--model",
        "-p",
        "--profile",
        "-s",
        "--sandbox",
        "-a",
        "--ask-for-approval",
        "--add-dir",
        "--enable",
        "--disable",
        "--remote",
        "--remote-auth-token-env",
        "-i",
        "--image",
        "--local-provider",
    }
)
CONFIGURATION_OPTIONS = frozenset(
    {"-c", "--config", "-p", "--profile", "--enable", "--disable"}
)
FLAG_OPTIONS = frozenset(
    {
        "--no-alt-screen",
        "--search",
        "--full-auto",
        "--dangerously-bypass-approvals-and-sandbox",
        "--no-daemon",
        "--strict-config",
        "--oss",
        "--approve-for-me",
        "--dangerously-bypass-hook-trust",
        "--worktree",
    }
)
DIRECTORY_OPTIONS = frozenset({"-C", "--cd"})
INFORMATION_OPTIONS = frozenset({"--help", "-h", "--version", "-V"})
MAINTENANCE_COMMANDS = frozenset(
    {
        "plugin",
        "login",
        "logout",
        "doctor",
        "help",
        "completion",
        "update",
        "mcp",
        "features",
    }
)
INVENTORY_CONFIG_KEYS = frozenset(
    {
        "model",
        "model_provider",
        "model_reasoning_effort",
        "model_reasoning_summary",
        "model_verbosity",
        "service_tier",
        "personality",
        "tui.terminal_title",
    }
)
NODE_SHIM_BYPASSED = True


def resolve_executable() -> str:
    return agent_cli.host_launch(HOST).resolve_executable()[0]


def installations(project: Path, entries: list) -> list:
    catalog.remember(entries)
    return entries


def location(entry: dict, project: Path) -> tuple[str, Path] | None:
    owner = entry.get("projectPath")
    if owner and entry.get("scope") not in ("user", "managed"):
        if not project.resolve().is_relative_to(Path(owner).resolve()):
            return None
    name, marketplace, version = (
        entry.get(key) for key in ("name", "marketplaceName", "version")
    )
    if not all(
        isinstance(value, str) and value for value in (name, marketplace, version)
    ):
        return None
    identity = f"{name}@{marketplace}"
    if (
        not required_plugins.IDENTITY.fullmatch(identity)
        or version in (".", "..")
        or "/" in version
        or "\\" in version
    ):
        return None
    return (
        identity,
        HOST.configuration_root / "plugins" / "cache" / marketplace / name / version,
    )


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


def ensure_hooks(
    project: Path,
    executable: str | None = None,
    *,
    running_session: bool = False,
    required_roots: Mapping[str, Path] | None = None,
) -> str | None:
    required = required_plugins.selected(required_plugins.project_root(project))
    if not required:
        return None
    binary = executable or resolve_executable()
    expected = {}
    roots = (
        required_plugins.check(project, HOST, binary)
        if required_roots is None
        else required_roots
    )
    for identity, root in (roots or {}).items():
        definitions = hook_trust.required_definitions(root, identity)
        if definitions:
            expected[identity] = definitions
    hook_trust.ensure_ready(binary, project.resolve(), expected)
    if running_session:
        try:
            hook_trust.reload_running(binary, project.resolve(), required=expected)
        except (OSError, RuntimeError) as error:
            return str(error)
    return None


def prepare_instructions(project: Path) -> None:
    override = project / "AGENTS.override.md"
    if override.is_file() and override.read_text(encoding="utf-8").strip():
        raise standards.StandardsError(
            f"{override} shadows the repository standards index"
        )


def install(project: Path, identities: Sequence[str]) -> None:
    project = required_plugins.project_root(project)
    requested = tuple(sorted(set(identities)))
    try:
        rows = installer_boundary.acquire_codex_cache(project, requested)
    except installer_boundary.InstallerBoundaryError as error:
        detail = ", ".join(requested)
        raise installer_boundary.InstallerBoundaryError(
            f"Codex cache acquisition for {project} ({detail}) failed: {error}"
        ) from error
    acquired = {
        row.get("pluginId")
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("pluginId"), str)
    }
    missing = sorted(set(requested) - acquired)
    if missing:
        raise installer_boundary.InstallerBoundaryError(
            f"Codex cache acquisition for {project} did not return: {', '.join(missing)}"
        )
    catalog.remember(rows, verified_install=True)
