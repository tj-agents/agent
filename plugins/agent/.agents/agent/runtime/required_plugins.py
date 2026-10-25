"""Require every repository-selected plugin in the active native host (Python 3.9+)."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys


IDENTITY = re.compile(r"[a-z][a-z0-9-]*@[a-z][a-z0-9-]*\Z")
INVENTORY_TIMEOUT_SECONDS = 5
MAINTENANCE_COMMANDS = {
    "codex": frozenset({"plugin", "login", "logout", "doctor", "help", "completion", "update", "mcp", "features"}),
    "claude": frozenset({"plugin", "plugins", "auth", "doctor", "install", "setup-token", "update", "upgrade", "mcp"}),
}
MODEL_CONFIG_KEYS = frozenset({
    "model", "model_provider", "model_reasoning_effort", "model_reasoning_summary",
    "model_verbosity", "service_tier", "personality",
})


class RequiredPluginsError(RuntimeError):
    pass


def project_root(project: Path) -> Path:
    project = project.resolve()
    for directory in (project, *project.parents):
        if (directory / ".agents/plugins.json").exists() or (directory / ".git").exists():
            return directory
    return project


def check_launch(project: Path, host: str, executable: str | None, arguments) -> Path | None:
    original_project = project
    value_options = {
        "-C", "--cd", "-c", "--config", "-m", "--model", "-p", "--profile",
        "-s", "--sandbox", "-a", "--ask-for-approval", "--add-dir", "--enable", "--disable",
        "--remote", "--remote-auth-token-env", "-i", "--image", "--local-provider",
    } if host == "codex" else {
        "--model", "--permission-mode", "--settings", "--setting-sources", "--plugin-dir", "--plugin-url",
    }
    configuration_options = {"-c", "--config", "-p", "--profile", "--enable", "--disable"} if host == "codex" else {
        "--settings", "--setting-sources", "--plugin-dir", "--plugin-url",
    }
    inventory_options = []
    flag_options = {
        "--no-alt-screen", "--search", "--full-auto", "--dangerously-bypass-approvals-and-sandbox",
        "--no-daemon", "--strict-config", "--oss", "--approve-for-me",
        "--dangerously-bypass-hook-trust", "--worktree",
    } if host == "codex" else set()
    command_seen = False
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            break
        if argument in ("--help", "-h", "--version", "-V" if host == "codex" else "-v"):
            return None
        if not command_seen and argument in MAINTENANCE_COMMANDS[host]:
            return None
        option, separator, value = argument.partition("=")
        if host == "codex" and len(argument) > 2 and argument[:2] in value_options:
            option, separator, value = argument[:2], "=", argument[2:].removeprefix("=")
        if option in value_options:
            if not separator:
                index += 1
                if index >= len(arguments):
                    raise RequiredPluginsError(f"{option} requires a value")
                value = arguments[index]
            if host == "codex" and option in ("-C", "--cd"):
                project = original_project / value
            if option in configuration_options:
                inventory_options.extend((option, value))
        elif argument not in flag_options:
            command_seen = True
        index += 1
    if inventory_options:
        check(project, host, executable, inventory_options=tuple(inventory_options))
    else:
        check(project, host, executable)
    return project.resolve()


def readable_payload(path: Path, host: str, name: str) -> bool:
    manifest = path / f".{host}-plugin" / "plugin.json"
    try:
        document = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if not isinstance(document, dict) or document.get("name") != name:
            return False
        for field in ("skills", "hooks", "agents", "commands"):
            values = document.get(field, [])
            if isinstance(values, str):
                values = [values]
            if field == "hooks" and isinstance(values, dict):
                continue
            if not isinstance(values, list):
                return False
            for value in values:
                if not isinstance(value, str):
                    return False
                target = (path / value).resolve()
                if not target.is_relative_to(path.resolve()) or not target.exists():
                    return False
                if target.is_file():
                    target.read_bytes()
                else:
                    next(target.iterdir(), None)
        return True
    except (OSError, ValueError):
        return False


def selected(project: Path) -> tuple[str, ...]:
    path = project / ".agents" / "plugins.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return ()
    except (OSError, ValueError) as error:
        raise RequiredPluginsError(f"Cannot read required plugin selection {path}: {error}") from error
    plugins = document.get("plugins") if isinstance(document, dict) and document.get("schema_version") == 1 else None
    if (not isinstance(plugins, list) or not plugins
            or any(not isinstance(value, str) or not IDENTITY.fullmatch(value) for value in plugins)):
        raise RequiredPluginsError(f"{path}: plugins must list required <plugin>@<marketplace> identities")
    return tuple(sorted(set(plugins)))


def enabled_roots(host: str, project: Path, entries: list) -> dict[str, Path]:
    roots = {}
    profile = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue
        if entry.get("installed") is False or entry.get("error") or entry.get("errors"):
            continue
        owner = entry.get("projectPath")
        if entry.get("scope") in ("project", "local") and not owner:
            continue
        if owner and not isinstance(owner, str):
            continue
        if owner and entry.get("scope") not in ("user", "managed"):
            if not project.resolve().is_relative_to(Path(owner).resolve()):
                continue
        if host == "codex":
            name, marketplace, version = (entry.get(key) for key in ("name", "marketplaceName", "version"))
            if not all(isinstance(value, str) and value for value in (name, marketplace, version)):
                continue
            identity = f"{name}@{marketplace}"
            if (not IDENTITY.fullmatch(identity) or version in (".", "..")
                    or "/" in version or "\\" in version):
                continue
            path = profile / "plugins" / "cache" / marketplace / name / version
        else:
            identity, location = entry.get("id"), entry.get("installPath")
            if not isinstance(identity, str) or not IDENTITY.fullmatch(identity) or not isinstance(location, str):
                continue
            path = Path(location)
        if readable_payload(path, host, identity.split("@", 1)[0]):
            roots[identity] = path
    return roots


def check(project: Path, host: str, executable: str | None = None,
          inventory_options: tuple[str, ...] = ()) -> dict[str, Path] | None:
    launch_directory = project.resolve()
    project = project_root(launch_directory)
    required = selected(project)
    if not required:
        return None
    if host == "codex":
        for option, value in zip(inventory_options[::2], inventory_options[1::2]):
            key = value.partition("=")[0].strip().strip("\"'")
            if option not in ("-c", "--config") or key not in MODEL_CONFIG_KEYS:
                raise RequiredPluginsError(
                    f"Cannot verify required Codex plugins with launch override {option}: native plugin "
                    "inventory does not apply session activation overrides. Use the persisted repository "
                    "configuration for plugin, profile and feature settings, then relaunch. "
                    "Model and reasoning overrides remain available."
                )
    binary = executable or shutil.which(host)
    if not binary:
        raise RequiredPluginsError(f"Required standards cannot be verified: {host} executable is unavailable")
    try:
        command = [binary, *inventory_options, "plugin", "list", "--json"]
        if os.name == "nt" and Path(binary).suffix.casefold() == ".ps1":
            command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", *command]
        result = subprocess.run(
            command, cwd=launch_directory, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=INVENTORY_TIMEOUT_SECONDS,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RequiredPluginsError(f"Required {host} plugin inventory could not run: {error}") from error
    if result.returncode:
        raise RequiredPluginsError(
            f"Required {host} plugin inventory failed: {(result.stderr or result.stdout).strip()[:1024]}"
        )
    try:
        data = json.loads(result.stdout)
    except ValueError as error:
        raise RequiredPluginsError(f"Required {host} plugin inventory returned invalid JSON") from error
    entries = data.get("installed") if host == "codex" and isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise RequiredPluginsError(f"Required {host} plugin inventory returned an invalid roster")
    roots = enabled_roots(host, launch_directory, entries)
    missing = sorted(set(required) - roots.keys())
    if missing:
        raise RequiredPluginsError(
            f"Required standards unavailable for {host} in {project}: {', '.join(missing)}. "
            "Repair repository selection/native installation, then start a fresh session. "
            "Cached files from another host do not establish activation."
        )
    return {identity: roots[identity] for identity in required}


CHECK_TIMEOUT_SECONDS = 90
ONBOARD_TIMEOUT_SECONDS = 660
MAX_OUTPUT = 1024


class PluginSetupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    identity: str
    script: Path
    declaration: str
    runtime: bytes


def bounded(result: subprocess.CompletedProcess) -> str:
    return ((result.stderr or result.stdout or f"exit {result.returncode}").strip()[:MAX_OUTPUT])


def private_path(root: Path, relative: str) -> Path:
    candidate = PurePosixPath(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts or candidate.suffix != ".py":
        raise PluginSetupError(f"invalid setup script declaration {relative!r}")
    path = root.joinpath(*candidate.parts)
    current = root
    if is_reparse(current):
        raise PluginSetupError(f"declared setup script is unsafe: {relative}")
    for part in candidate.parts:
        current /= part
        if is_reparse(current):
            raise PluginSetupError(f"declared setup script is unsafe: {relative}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PluginSetupError(f"declared setup script is unavailable: {relative}") from error
    if not resolved.is_relative_to(root.resolve()) or is_reparse(path) or not resolved.is_file():
        raise PluginSetupError(f"declared setup script is unsafe: {relative}")
    return resolved


def is_reparse(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def runtime_bytes(root: Path) -> bytes:
    if is_reparse(root):
        raise PluginSetupError("plugin setup runtime directory is a reparse point")
    digest = hashlib.sha256()
    directories = [root]
    while directories:
        directory = directories.pop()
        for path in sorted(directory.iterdir(), reverse=True):
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts or path.suffix == ".pyc":
                continue
            if is_reparse(path):
                raise PluginSetupError(f"plugin setup runtime contains a reparse point: {relative}")
            if path.is_dir():
                directories.append(path)
            elif path.is_file():
                digest.update(relative.as_posix().encode() + b"\0" + path.read_bytes())
    return digest.digest()


def harness_document(root: Path) -> tuple[dict, str]:
    harness = root / "harness.json"
    if not harness.is_file() or is_reparse(harness):
        raise PluginSetupError("verified runtime has no harness declaration")
    try:
        document = json.loads(harness.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PluginSetupError("cannot read harness declaration") from error
    plugin = document.get("plugin") if isinstance(document, dict) else None
    marketplace, separator, name = plugin.partition("/") if isinstance(plugin, str) else ("", "", "")
    identity = f"{name}@{marketplace}"
    if not separator or not IDENTITY.fullmatch(identity):
        raise PluginSetupError("harness plugin identity is invalid")
    return document, identity


def declaration(root: Path, identity: str, document: dict | None = None) -> Target | None:
    name, marketplace = identity.split("@", 1)
    document, declared = harness_document(root) if document is None else (document, identity)
    if declared != identity:
        raise PluginSetupError(f"{identity}: harness identity does not match the selected plugin")
    requires = document.get("requires", {})
    if not isinstance(requires, dict):
        raise PluginSetupError(f"{identity}: harness requires declaration is invalid")
    setup = requires.get("setup")
    if setup is None:
        return None
    if not isinstance(setup, dict) or set(setup) != {"script"} or not isinstance(setup["script"], str):
        raise PluginSetupError(f"{identity}: requires.setup must be {{\"script\": \"relative/path.py\"}}")
    script = private_path(root, setup["script"])
    return Target(identity, script, setup["script"], runtime_bytes(script.parent))


def targets(project: Path, host: str, executable: str | None) -> list[Target]:
    roots = check(project, host, executable)
    if roots is None:
        return []
    identities = selected(project_root(project))
    found = []
    for identity in identities:
        root = roots.get(identity)
        if root is None:
            raise PluginSetupError(f"{identity}: verified native installation is unavailable")
        document, declared = harness_document(root)
        if declared != identity:
            raise PluginSetupError(f"{identity}: harness identity does not match the selected plugin")
        target = declaration(root, identity, document)
        if target is not None:
            found.append(target)
    return found


def collect(project: Path, hosts: tuple[str, ...], executables: dict[str, str | None]) -> tuple[list[Target], list[str]]:
    discovered, errors = {}, []
    for host in hosts:
        try:
            discovered[host] = {target.identity: target for target in targets(project, host, executables.get(host))}
        except (RequiredPluginsError, PluginSetupError) as error:
            errors.append(f"{host}: {error}")
    if not errors and discovered:
        reference_host = next(iter(discovered))
        reference = discovered[reference_host]
        for host, current in discovered.items():
            if set(current) != set(reference):
                errors.append(f"{host}: native host setup declarations disagree with {reference_host}")
                continue
            for identity, target in current.items():
                expected = reference[identity]
                if expected.declaration != target.declaration or expected.runtime != target.runtime:
                    errors.append(f"{identity}: native host runtimes disagree on its setup declaration")
    return ([] if errors else [reference[key] for key in sorted(reference)]), errors


def invoke(target: Target, arguments: list[str], timeout: int, inherit: bool = False) -> subprocess.CompletedProcess:
    command = [sys.executable, "-B", str(target.script), "setup", *arguments]
    if inherit:
        return subprocess.run(command, timeout=timeout, check=False)
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, check=False)


def check_setup(project: Path, hosts: tuple[str, ...], executables: dict[str, str | None]) -> list[str]:
    targets_found, errors = collect(project, hosts, executables)
    for target in targets_found:
        try:
            result = invoke(target, ["--check"], CHECK_TIMEOUT_SECONDS)
            if result.returncode:
                errors.append(f"{target.identity}: setup --check failed: {bounded(result)}")
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"{target.identity}: setup --check could not run: {error}")
    return errors


def converge_setup(project: Path, hosts: tuple[str, ...], executables: dict[str, str | None], onboard: bool = False) -> list[str]:
    targets_found, errors = collect(project, hosts, executables)
    for target in targets_found:
        try:
            checked = invoke(target, ["--check"], CHECK_TIMEOUT_SECONDS)
            if checked.returncode:
                applied = invoke(target, ["--onboard"] if onboard else [],
                                 ONBOARD_TIMEOUT_SECONDS if onboard else CHECK_TIMEOUT_SECONDS, inherit=onboard)
                if applied.returncode:
                    errors.append(f"{target.identity}: setup failed: {bounded(applied)}")
                    continue
                checked = invoke(target, ["--check"], CHECK_TIMEOUT_SECONDS)
            if checked.returncode:
                errors.append(f"{target.identity}: setup did not converge: {bounded(checked)}")
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"{target.identity}: setup could not run: {error}")
    return errors
