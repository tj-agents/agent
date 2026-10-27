"""Shared dispatch for terminal-profile Claude and Codex adapters."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Mapping

import agent_cli
from host import Host
from host_plugins import PLUGINS


def native_host(name: str) -> Host | None:
    try:
        return Host(name)
    except ValueError:
        return None


def native_shim_kind(target: str) -> Host | None:
    """Return the supported CLI named by a Windows launcher shim, if any."""
    name = target.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if not name.endswith((".cmd", ".bat", ".ps1")):
        return None
    return native_host(name.rsplit(".", 1)[0])


def native_target_for_shim(target: str) -> str:
    """Resolve a supported CLI shim to its native executable."""
    kind = native_shim_kind(target)
    if target.replace("\\", "/").rsplit("/", 1)[-1].casefold().endswith((".cmd", ".bat")) and kind is None:
        raise OSError(f"unsupported batch launcher cannot run safely: {target}")
    if kind is None:
        return target
    try:
        return PLUGINS[kind].resolve_executable()
    except agent_cli.LaunchError as error:
        raise OSError(f"{kind} launcher shim cannot run safely: {error}") from None


def command_for_target(target: str, arguments: list[str]) -> list[str]:
    """Build the platform-required argument list without changing the typed arguments."""
    suffix = Path(target).suffix.casefold()
    resolved_target = Path(shutil.which(target) or target)
    host = native_host(Path(target).name)
    if (os.name != "nt" and host is not None and PLUGINS[host].NODE_SHIM_BYPASSED
            and resolved_target.resolve().suffix == ".js"):
        return [PLUGINS[host].resolve_executable(), *arguments]
    if os.name == "nt" and native_shim_kind(target):
        return [native_target_for_shim(target), *arguments]
    if os.name == "nt" and suffix == ".ps1":
        shell = shutil.which("pwsh") or shutil.which("powershell.exe")
        if not shell:
            raise OSError("PowerShell is required to run the selected CLI launcher")
        return [shell, "-NoProfile", "-File", target, *arguments]
    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        native = native_target_for_shim(target)
        return [native, *arguments]
    return [target, *arguments]


BINDING_STATE_VARIABLES = (
    "TJ_AGENTS_STARTUP_ACK_DIRECTORY",
    "TJ_AGENTS_STARTUP_ACK_TOKEN",
    "TJ_AGENTS_STARTUP_REQUIRED_HOOKS",
    "TJ_AGENTS_STARTUP_SNAPSHOT_DIRECTORY",
)


def without_binding_state(environment: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in environment.items() if key not in BINDING_STATE_VARIABLES}


def run_target(target: str, arguments: list[str], environment: Mapping[str, str] | None = None) -> int:
    try:
        return subprocess.run(command_for_target(target, arguments), env=environment).returncode
    except OSError as error:
        print(f"terminal launcher could not start {target}: {error}", file=sys.stderr)
        return 127


def flush_before_cli() -> None:
    sys.stdout.flush()
    sys.stderr.flush()
