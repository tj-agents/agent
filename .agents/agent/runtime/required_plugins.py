"""Require every repository-selected plugin in the active native host (Python 3.9+)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time

from tj_cli import process
from tj_agent.host import Host
import host_plugins


IDENTITY = re.compile(r"[a-z][a-z0-9-]*@[a-z][a-z0-9-]*\Z")
INVENTORY_TIMEOUT_SECONDS = 20
MARKETPLACE_NAME = "tj-agents"
MARKETPLACE_REPOSITORY = "tj-agents/marketplace"
MARKETPLACE_SPARSE_PATHS = (".agents/plugins", ".claude-plugin", "plugins")


def producer_repository(repository: object) -> bool:
    return (isinstance(repository, str) and repository.casefold().startswith("tj-agents/")
            and repository.casefold() != MARKETPLACE_REPOSITORY)


def marketplace_error(name: str, repository: object) -> str | None:
    canonical_repository = isinstance(repository, str) and repository.casefold() == MARKETPLACE_REPOSITORY
    if ((name == MARKETPLACE_NAME and repository != MARKETPLACE_REPOSITORY)
            or (name != MARKETPLACE_NAME and canonical_repository)):
        return f"The {MARKETPLACE_NAME} marketplace must be {MARKETPLACE_REPOSITORY} and nothing else may use that name"
    if producer_repository(repository):
        return f"{repository} is published through the {MARKETPLACE_NAME} marketplace; select {MARKETPLACE_REPOSITORY}"
    return None


class RequiredPluginsError(RuntimeError):
    pass


class MissingRequiredPluginsError(RequiredPluginsError):
    def __init__(self, message: str, project: Path, missing: Sequence[str]) -> None:
        super().__init__(message)
        self.project = project
        self.missing = tuple(missing)


def project_root(project: Path) -> Path:
    project = project.resolve()
    for directory in (project, *project.parents):
        if (directory / ".agents/plugins.json").exists() or (directory / ".git").exists():
            return directory
    return project


def launch_context(project: Path, host: Host, arguments) -> tuple[Path | None, tuple[str, ...]]:
    plugins = host_plugins.PLUGINS[host]
    original_project = project
    inventory_options = []
    command_seen = False
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            break
        if argument in plugins.INFORMATION_OPTIONS:
            return None, ()
        if not command_seen and argument in plugins.MAINTENANCE_COMMANDS:
            return None, ()
        option, separator, value = argument.partition("=")
        if len(argument) > 2 and argument[:2] in plugins.VALUE_OPTIONS:
            option, separator, value = argument[:2], "=", argument[2:].removeprefix("=")
        if option in plugins.VALUE_OPTIONS:
            if not separator:
                index += 1
                if index >= len(arguments):
                    raise RequiredPluginsError(f"{option} requires a value")
                value = arguments[index]
            if option in plugins.DIRECTORY_OPTIONS:
                project = original_project / value
            if option in plugins.CONFIGURATION_OPTIONS:
                inventory_options.extend((option, value))
        elif argument not in plugins.FLAG_OPTIONS:
            command_seen = True
        index += 1
    return project.resolve(), tuple(inventory_options)


def check_launch_roots(project: Path, host: Host, executable: str | None, arguments) -> tuple[Path | None, dict[str, Path] | None]:
    effective_project, inventory_options = launch_context(project, host, arguments)
    if effective_project is None:
        return None, None
    return effective_project, check(effective_project, host, executable, inventory_options=inventory_options)


def check_launch(project: Path, host: Host, executable: str | None, arguments) -> Path | None:
    return check_launch_roots(project, host, executable, arguments)[0]


def readable_payload(path: Path, host: Host, name: str) -> bool:
    manifest = path / host.manifest_directory / "plugin.json"
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


def enabled_roots(plugins: host_plugins.Plugins, project: Path, entries: list) -> dict[str, Path]:
    roots = {}
    for entry in plugins.installations(project, entries):
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue
        if entry.get("installed") is False or entry.get("error") or entry.get("errors"):
            continue
        owner = entry.get("projectPath")
        if entry.get("scope") in ("project", "local") and not owner:
            continue
        if owner and not isinstance(owner, str):
            continue
        located = plugins.location(entry, project)
        if located is None:
            continue
        identity, path = located
        if readable_payload(path, plugins.HOST, identity.split("@", 1)[0]):
            roots[identity] = path
    return roots


def check(project: Path, host: Host, executable: str | None = None,
          inventory_options: tuple[str, ...] = ()) -> dict[str, Path] | None:
    plugins = host_plugins.PLUGINS[host]
    launch_directory = project.resolve()
    project = project_root(launch_directory)
    required = selected(project)
    if not required:
        return None
    plugins.require_inventory_options(inventory_options)
    binary = executable or shutil.which(host.value)
    if not binary:
        raise RequiredPluginsError(f"Required standards cannot be verified: {host} executable is unavailable")
    roots = inventory_roots(launch_directory, host, binary, inventory_options, identities=required)
    missing = sorted(set(required) - roots.keys())
    if missing:
        raise MissingRequiredPluginsError(
            f"Required standards unavailable for {host} in {project}: {', '.join(missing)}. "
            "Run tj install --step 'plugin setup' in this project to install the required plugins. "
            "Cached files from another host do not establish activation.", project, missing,
        )
    return {identity: roots[identity] for identity in required}


def inventory_roots(project: Path, host: Host, executable: str | None = None,
                    inventory_options: tuple[str, ...] = (), *, identities: Sequence[str] | None = None) -> dict[str, Path]:
    """Return verified enabled roots, optionally limited to selected marketplaces and identities."""
    plugins = host_plugins.PLUGINS[host]
    launch_directory = project.resolve()
    binary = executable or shutil.which(host.value)
    if not binary:
        raise RequiredPluginsError(f"Required standards cannot be verified: {host} executable is unavailable")
    if identities is None:
        return enabled_roots(plugins, launch_directory, inventory(binary, plugins, launch_directory, inventory_options))
    plugins.require_inventory_options(inventory_options)
    entries = []
    deadline = time.monotonic() + INVENTORY_TIMEOUT_SECONDS
    for marketplace in plugins.inventory_marketplaces(identities):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RequiredPluginsError(f"Required {host} plugin inventory exceeded its {INVENTORY_TIMEOUT_SECONDS}-second budget")
        entries.extend(inventory(binary, plugins, launch_directory, inventory_options, marketplace, timeout=remaining))
    return {identity: root for identity, root in enabled_roots(plugins, launch_directory, entries).items()
            if identity in identities}


def inventory(binary: str, plugins: host_plugins.Plugins, launch_directory: Path, inventory_options: tuple[str, ...],
              marketplace: str | None = None, *, timeout: float | None = None) -> list:
    command = [binary, *inventory_options, "plugin", "list"]
    if marketplace is not None:
        command.extend(("--marketplace", marketplace))
    command.append("--json")
    try:
        if os.name == "nt" and Path(binary).suffix.casefold() == ".ps1":
            command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", *command]
        result = process.run(
            command, cwd=launch_directory, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=INVENTORY_TIMEOUT_SECONDS if timeout is None else timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        scope = f" for marketplace {marketplace}" if marketplace is not None else ""
        raise RequiredPluginsError(f"Required {plugins.HOST} plugin inventory{scope} could not run: {error}") from error
    if result.returncode:
        scope = f" for marketplace {marketplace}" if marketplace is not None else ""
        raise RequiredPluginsError(
            f"Required {plugins.HOST} plugin inventory{scope} failed: {(result.stderr or result.stdout).strip()[:1024]}"
        )
    try:
        data = json.loads(result.stdout)
    except ValueError as error:
        raise RequiredPluginsError(f"Required {plugins.HOST} plugin inventory returned invalid JSON") from error
    entries = plugins.inventory_entries(data)
    if not isinstance(entries, list):
        raise RequiredPluginsError(f"Required {plugins.HOST} plugin inventory returned an invalid roster")
    return entries


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
    converge: bool


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
    valid_keys = ({"script"}, {"script", "converge"})
    if (
        not isinstance(setup, dict)
        or set(setup) not in valid_keys
        or not isinstance(setup["script"], str)
        or ("converge" in setup and not isinstance(setup["converge"], bool))
    ):
        raise PluginSetupError(
            f'{identity}: requires.setup must be {{"script": "relative/path.py"}}, '
            'optionally with "converge": a bool'
        )
    script = private_path(root, setup["script"])
    converge = bool(setup.get("converge", False))
    return Target(
        identity, script, setup["script"], runtime_bytes(script.parent), converge
    )


def converges(root: Path) -> bool:
    try:
        document, _ = harness_document(root)
    except PluginSetupError:
        return False
    requires = document.get("requires")
    setup = requires.get("setup") if isinstance(requires, dict) else None
    return isinstance(setup, dict) and setup.get("converge") is True


def targets(project: Path, host: Host, executable: str | None) -> list[Target]:
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


def collect(project: Path, hosts: Sequence[Host], executables: Mapping[Host, str | None]) -> tuple[list[Target], list[str]]:
    discovered, errors = {}, []
    for host in hosts:
        try:
            discovered[host] = {target.identity: target for target in targets(project, host, executables.get(host))}
        except (RequiredPluginsError, PluginSetupError, RuntimeError, OSError) as error:
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
                disagree = (
                    expected.declaration != target.declaration
                    or expected.runtime != target.runtime
                    or expected.converge != target.converge
                )
                if disagree:
                    errors.append(f"{identity}: native host runtimes disagree on its setup declaration")
    return ([] if errors else [reference[key] for key in sorted(reference)]), errors


def invoke(target: Target, arguments: list[str], timeout: int, project: Path,
           inherit: bool = False) -> subprocess.CompletedProcess:
    command = [sys.executable, "-B", str(target.script), "setup", *arguments]
    if inherit:
        return process.run(command, cwd=project, timeout=timeout, check=False)
    return process.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=project, timeout=timeout, check=False)


def reconcile_selection(project: Path, *, check_only: bool = False) -> list[str]:
    return reconcile_selections([project], check_only=check_only)


def reconcile_selections(projects: Sequence[Path], *, check_only: bool = False) -> list[str]:
    projects = sorted({project_root(project) for project in projects
                       if (project_root(project) / ".agents/plugins.json").is_file()})
    if not projects:
        return []
    script = Path(__file__).resolve().parents[1] / "utility/plugins/scripts/adopt_plugins.py"
    additional = [argument for project in projects[1:] for argument in ("--also-root", str(project))]
    try:
        result = process.run(
            [sys.executable, "-B", str(script), "--root", str(projects[0]), "--converge-existing",
             "--mode", "check" if check_only else "write", *additional],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RequiredPluginsError(f"Repository plugin selection could not converge: {error}") from error
    detail = (result.stderr or result.stdout).strip()
    if result.returncode:
        if check_only and result.returncode == 1 and result.stdout.startswith("Drift in:"):
            return [detail]
        raise RequiredPluginsError(f"Repository plugin selection could not converge: {detail}")
    if detail and not check_only:
        print(f"plugins: {detail}", file=sys.stderr)
    return []


def check_setup(project: Path, hosts: Sequence[Host], executables: Mapping[Host, str | None]) -> list[str]:
    project = project_root(project)
    try:
        drift = reconcile_selection(project, check_only=True)
    except RequiredPluginsError as error:
        return [str(error)]
    if drift:
        return drift
    targets_found, errors = collect(project, hosts, executables)
    for target in targets_found:
        try:
            result = invoke(target, ["--check"], CHECK_TIMEOUT_SECONDS, project)
            if result.returncode:
                errors.append(f"{target.identity}: setup --check failed: {bounded(result)}")
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"{target.identity}: setup --check could not run: {error}")
    return errors


def converge_setup(project: Path, hosts: Sequence[Host], executables: Mapping[Host, str | None], onboard: bool = False) -> list[str]:
    project = project_root(project)
    try:
        reconcile_selection(project)
    except RequiredPluginsError as error:
        return [str(error)]
    targets_found, errors = collect(project, hosts, executables)
    for target in targets_found:
        try:
            checked = invoke(target, ["--check"], CHECK_TIMEOUT_SECONDS, project)
            if checked.returncode:
                applied = invoke(target, ["--onboard"] if onboard else [],
                                 ONBOARD_TIMEOUT_SECONDS if onboard else CHECK_TIMEOUT_SECONDS, project, inherit=onboard)
                if applied.returncode:
                    errors.append(f"{target.identity}: setup failed: {bounded(applied)}")
                    continue
                checked = invoke(target, ["--check"], CHECK_TIMEOUT_SECONDS, project)
            if checked.returncode:
                errors.append(f"{target.identity}: setup did not converge: {bounded(checked)}")
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"{target.identity}: setup could not run: {error}")
    return errors
