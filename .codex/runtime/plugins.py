"""Codex's native plugin selection, refresh and inventory (Python 3.9+)."""

from __future__ import annotations

from pathlib import Path

import agent_cli
from codex import hook_trust
from host import Host
import plugin_autoupdate
import plugin_refresh
import required_plugins
import session_relaunch

HOST = Host.CODEX
CATALOG = ".agents/plugins/marketplace.json"
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
MODEL_CONFIG_KEYS = frozenset({
    "model", "model_provider", "model_reasoning_effort", "model_reasoning_summary",
    "model_verbosity", "service_tier", "personality",
})
NODE_SHIM_BYPASSED = True
reconcile = None


def resolve_executable() -> str:
    return agent_cli.resolve_codex_executable()[0]


def selection(executable: str, project: Path) -> tuple[dict[str, str | None], list[str]]:
    with hook_trust.AppServer(executable, project) as server:
        response = server.request("config/read", {"cwd": str(project), "includeLayers": True})
    config = response["config"]
    return ({name: plugin_refresh.repository(source.get("source"))
             for name, source in config.get("marketplaces", {}).items() if source.get("source_type") == "git"},
            [name for name, flag in config.get("plugins", {}).items()
             if isinstance(flag, dict) and flag.get("enabled") is True])


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
        if option not in ("-c", "--config") or key not in MODEL_CONFIG_KEYS:
            raise required_plugins.RequiredPluginsError(
                f"Cannot verify required Codex plugins with launch override {option}: native plugin "
                "inventory does not apply session activation overrides. Use the persisted repository "
                "configuration for plugin, profile and feature settings, then relaunch. "
                "Model and reasoning overrides remain available."
            )


def inventory_marketplaces(required: tuple[str, ...]) -> list[str | None]:
    return sorted({identity.rsplit("@", 1)[1] for identity in required})


def inventory_entries(data: object) -> object:
    return data.get("installed") if isinstance(data, dict) else data


def entry_errors(entries: list, project: Path) -> list[str]:
    return []


def marketplace_location(executable: str, project: Path, name: str) -> str | None:
    with hook_trust.AppServer(executable, project) as server:
        listing = server.request("plugin/list", {"cwds": [str(project)]})
    return next((str(Path(market["path"]).parents[2]) for market in listing.get("marketplaces", [])
                 if market.get("name") == name), None)


def install_path(entry: dict, marketplace: str, plugin: str) -> Path:
    return HOST.configuration_root / "plugins/cache" / marketplace / plugin / (entry.get("version") or "local")


def refresh_marketplaces(executable: str, project: Path, marketplaces: dict) -> None:
    for name in marketplaces:
        plugin_refresh.run([executable, "plugin", "marketplace", "upgrade", name], project)


def install_missing(executable: str, project: Path, missing) -> None:
    user_config = HOST.configuration_root / "config.toml"
    user_enabled = plugin_autoupdate.enabled_identities(plugin_autoupdate.load_toml(user_config))
    for identity in missing:
        plugin_refresh.run([executable, "plugin", "add", identity], project)
    plugin_autoupdate.remove_user_plugins(user_config, set(missing) - user_enabled)


def refresh_plugins(executable: str, project: Path, marketplaces: dict, plugins: list[str]) -> list[str]:
    install_missing(executable, project, plugins)
    return []


def update(repositories: set[str] | None, project: Path, updated: set[str] | None = None) -> bool:
    arguments = ["--no-sweep", "--no-self-update", "--retry-failures"]
    for repository in sorted(repositories or ()):
        arguments += ["--repository", repository]
    return plugin_autoupdate.main(arguments) == 0


def ensure_hooks(project: Path, executable: str | None = None, *, running_session: bool = False) -> str | None:
    required = required_plugins.selected(required_plugins.project_root(project))
    if not required:
        return None
    binary = executable or resolve_executable()
    expected = {}
    roots = required_plugins.check(project, HOST, binary)
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


def adopt(project: Path, updated: set[str], reason: str | None, allowed: bool) -> dict | None:
    if reason is None:
        return None
    if not allowed:
        raise plugin_refresh.RefreshError("Codex requires client restart, but --no-relaunch forbids it: " + reason)
    result = session_relaunch.relaunch(project, {}, host=HOST)
    if result.get("relaunch") != "armed":
        raise plugin_refresh.RefreshError("The owning Codex client restart was not armed")
    return {**result, "reason": reason, "adoption": "pending"}
