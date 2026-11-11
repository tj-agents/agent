"""Claude's native plugin inventory contract (Python 3.9+)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import functools
import json
import os
from pathlib import Path
import subprocess

import agent_cli
from tj_agent.host import Host
import required_plugins
import standards
from tj_cli import process

HOST = Host.CLAUDE
VALUE_OPTIONS = frozenset({
    "--model", "--permission-mode", "--settings", "--setting-sources", "--plugin-dir", "--plugin-url",
})
CONFIGURATION_OPTIONS = frozenset({"--settings", "--setting-sources", "--plugin-dir", "--plugin-url"})
FLAG_OPTIONS: frozenset[str] = frozenset()
DIRECTORY_OPTIONS: frozenset[str] = frozenset()
INFORMATION_OPTIONS = frozenset({"--help", "-h", "--version", "-v"})
MAINTENANCE_COMMANDS = frozenset({
    "plugin", "plugins", "auth", "doctor", "install", "setup-token", "update", "upgrade", "mcp",
})
GIT_TIMEOUT_SECONDS = 5
NODE_SHIM_BYPASSED = False


def resolve_executable() -> str:
    return agent_cli.host_launch(HOST).resolve_executable()


@functools.lru_cache(maxsize=None)
def git_common_directory(project: Path) -> str | None:
    if not project.is_dir() or not any((directory / ".git").exists() for directory in (project, *project.parents)):
        return None
    try:
        result = process.run(['git', '-C', str(project), 'rev-parse', '--git-common-dir'],
                             capture_output=True, text=True, encoding='utf-8', errors='replace',
                             timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        raise required_plugins.RequiredPluginsError(
            f'Cannot resolve the native Claude repository for {project}: {error}') from error
    if result.returncode or not result.stdout.strip():
        raise required_plugins.RequiredPluginsError(
            f'Cannot resolve the native Claude repository for {project}: {result.stderr.strip()}')
    return os.path.normcase(str((project / result.stdout.strip()).resolve()))


def installations(project: Path, entries: list) -> list:
    if not entries:
        return []
    project = project.resolve()
    common = git_common_directory(project)
    selected, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        scope, owner = entry.get('scope'), entry.get('projectPath')
        if scope in {'project', 'local'}:
            if not isinstance(owner, str) or not owner:
                continue
            owner = Path(owner).resolve()
            try:
                owner_common = git_common_directory(owner)
            except required_plugins.RequiredPluginsError:
                continue
            if common and owner_common:
                if common != owner_common:
                    continue
            elif not project.is_relative_to(owner):
                continue
            key = entry.get('id'), scope
            if key in seen:
                continue
            seen.add(key)
        elif owner and scope not in {'user', 'managed'}:
            if not isinstance(owner, str) or not project.is_relative_to(Path(owner).resolve()):
                continue
        selected.append(entry)
    return selected


def location(entry: dict, project: Path) -> tuple[str, Path] | None:
    identity, path = entry.get("id"), entry.get("installPath")
    if not isinstance(identity, str) or not required_plugins.IDENTITY.fullmatch(identity) or not isinstance(path, str):
        return None
    return identity, Path(path)


def require_inventory_options(options: tuple[str, ...]) -> None:
    return None


def inventory_marketplaces(required: tuple[str, ...]) -> tuple[str | None, ...]:
    return (None,)


def inventory_entries(data: object) -> object:
    return data


def ensure_hooks(project: Path, executable: str | None = None, *, running_session: bool = False,
                 required_roots: Mapping[str, Path] | None = None) -> str | None:
    return None


def prepare_instructions(project: Path) -> None:
    pointer = project / "CLAUDE.md"
    shared = project / "AGENTS.md"
    for path in (pointer, shared):
        if path.is_symlink() or path.is_junction():
            raise standards.StandardsError(
                f"Refusing redirected instruction file: {path}"
            )
    original = pointer.read_text(encoding="utf-8") if pointer.exists() else ""
    if original.strip() == "@AGENTS.md":
        return
    if original.strip():
        current = shared.read_text(encoding="utf-8") if shared.exists() else ""
        content = "\n".join(
            line for line in original.splitlines() if line.strip() != "@AGENTS.md"
        ).strip()
        shared.write_text(current.rstrip() + "\n\n" + content + "\n", encoding="utf-8")
    pointer.write_text("@AGENTS.md\n", encoding="utf-8")


def user_enabled() -> Mapping[str, object]:
    try:
        settings = json.loads((HOST.configuration_root / "settings.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    enabled = settings.get("enabledPlugins") if isinstance(settings, dict) else None
    return enabled if isinstance(enabled, dict) else {}


def plugin_command(project: Path, identity: str, *arguments: str) -> None:
    result = process.run(
        ["claude", "plugin", *arguments, identity, "--scope", "user", "--json", "-y"],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise required_plugins.RequiredPluginsError(
            f"Cannot {arguments[0]} {identity} for {project}: {detail}"
        )


def install(project: Path, identities: Sequence[str]) -> None:
    enabled = user_enabled()
    for identity in identities:
        plugin_command(project, identity, "install")
        if enabled.get(identity) is not True:
            plugin_command(project, identity, "disable")
