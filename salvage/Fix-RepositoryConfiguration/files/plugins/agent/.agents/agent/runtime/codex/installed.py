"""Codex's installed plugin build labels, and their refresh (Python 3.9+)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Protocol

import installer_boundary
import jobs
import plugin_activation
import required_plugins
import standards
from codex import catalog, hook_trust, launch
from launch_errors import LaunchError
from tj_agent.host import Host
from tj_cli import process
from tree_digest import Digests

HOST = Host.CODEX
RELOAD = "restart this session"
INSTALL_TIMEOUT_SECONDS = 600


class Server(Protocol):
    def request(self, method: str, params: dict[str, object]) -> dict[str, object]: ...


def loaded_directories(server: Server) -> set[Path]:
    directories = set()
    seen = set()
    cursor: str | None = None
    while True:
        page = server.request("thread/loaded/list", {"cursor": cursor})
        threads = page.get("data")
        if not isinstance(threads, list) or any(
            not isinstance(thread, str) for thread in threads
        ):
            raise RuntimeError("Codex returned an invalid loaded-thread list")
        for identity in threads:
            thread = server.request("thread/read", {"threadId": identity}).get("thread")
            directory = thread.get("cwd") if isinstance(thread, dict) else None
            if not isinstance(directory, str) or not Path(directory).is_absolute():
                raise RuntimeError(
                    f"Codex thread {identity} has no absolute working directory"
                )
            directories.add(Path(directory))
        next_cursor = page.get("nextCursor")
        if next_cursor is None:
            return directories
        if not isinstance(next_cursor, str) or next_cursor in seen:
            raise RuntimeError("Codex returned an invalid loaded-thread cursor")
        seen.add(next_cursor)
        cursor = next_cursor


def update_project_standards(directory: Path) -> None:
    project = required_plugins.project_root(directory)
    if (project / ".agents/repo-capabilities.local.json").is_file():
        return
    selected = required_plugins.selected(project)
    if not selected:
        return
    builds = catalog.recorded(jobs.state_directory())
    digests = Digests(jobs.state_directory())
    roots = {}
    for identity in selected:
        plugin, _, marketplace = identity.rpartition("@")
        build = builds.get(identity)
        root = catalog.root(marketplace, plugin, build.version) if build else None
        if root is None or not catalog.current(marketplace, plugin, digests):
            raise standards.StandardsError(
                f"Cannot verify updated standards for {identity} in {project}"
            )
        roots[identity] = root
    digests.save()
    standards.update(project, roots)


def identities() -> set[str]:
    project = required_plugins.project_root(Path.cwd())
    return set(required_plugins.selected(project))


def selected_identities(marketplaces: tuple[str, ...]) -> tuple[str, ...]:
    project = required_plugins.project_root(Path.cwd())
    return tuple(
        identity
        for identity in required_plugins.selected(project)
        if identity.rpartition("@")[2] in marketplaces
    )


def refresh_identities(marketplaces: tuple[str, ...]) -> set[str]:
    return set(selected_identities(marketplaces))


def behind(marketplaces: tuple[str, ...]) -> list[str]:
    digests = Digests(jobs.state_directory())
    found = []
    for identity in selected_identities(marketplaces):
        plugin, _, marketplace = identity.rpartition("@")
        if not catalog.current(marketplace, plugin, digests):
            found.append(
                f"{identity}: installed build does not match its verified Codex catalog version"
            )
    digests.save()
    return found


def current(marketplace: str, plugin: str, digests: Digests) -> bool:
    return catalog.current(marketplace, plugin, digests)


def running_behind(root: Path) -> str | None:
    build = catalog.recorded(jobs.state_directory()).get("agent@tj-agents")
    loaded = catalog.label(root)
    if build is not None and loaded != build.version:
        return f"agent@tj-agents: this session loaded build {loaded or 'that was since replaced'}, current {build.version}"
    return None


def selection_sources(project: Path, identities: tuple[str, ...]) -> dict[str, str]:
    document = json.loads(
        (project / ".agents" / "plugins.json").read_text(encoding="utf-8")
    )
    marketplaces = document.get("marketplaces") if isinstance(document, dict) else None
    if not isinstance(marketplaces, dict):
        raise ValueError(f"{project}: selected plugins have no marketplace sources")
    required = required_plugins.selected(project)
    if tuple(sorted(set(identities))) != identities or not set(identities).issubset(
        required
    ):
        raise ValueError(
            f"{project}: requested plugins do not match its declared selection"
        )
    sources = {}
    for identity in identities:
        marketplace = identity.rpartition("@")[2]
        source = marketplaces.get(marketplace)
        repository = source.get("repository") if isinstance(source, dict) else None
        if not isinstance(repository, str) or not repository:
            raise ValueError(f"{project}: {marketplace} has no repository source")
        sources[marketplace] = repository
    return sources


def reject_reparse_or_hardlink(
    path: Path, label: str, *, directory: bool = False
) -> None:
    if required_plugins.is_reparse(path):
        raise ValueError(f"{label} is a symbolic link or junction: {path}")
    if not directory and path.is_file() and path.stat().st_nlink != 1:
        raise ValueError(f"{label} has multiple hard links: {path}")


def exact_project(project: Path) -> Path:
    candidate = Path(project)
    reject_reparse_or_hardlink(candidate, "repository root", directory=True)
    requested = candidate.resolve(strict=True)
    root = required_plugins.project_root(requested)
    if root != requested:
        raise ValueError(
            f"Codex cache acquisition requires the exact repository root: {root}"
        )
    selection = root / ".agents" / "plugins.json"
    for path, label, directory in (
        (root, "repository root", True),
        (root / ".agents", "repository settings directory", True),
        (selection, "repository plugin selection", False),
    ):
        reject_reparse_or_hardlink(path, label, directory=directory)
    return root


def cache_path(root: Path, identity: str, version: str) -> Path:
    plugin, separator, marketplace = identity.partition("@")
    if not separator or not all(
        part and part not in {".", ".."} and "/" not in part and "\\" not in part
        for part in (marketplace, plugin, version)
    ):
        raise ValueError(f"Invalid Codex cache identity: {identity}")
    return root / "plugins" / "cache" / marketplace / plugin / version


def inspect_tree(root: Path, label: str) -> None:
    reject_reparse_or_hardlink(root, label, directory=True)
    for path in root.rglob("*"):
        reject_reparse_or_hardlink(path, label, directory=path.is_dir())


def entry_root(
    temporary_home: Path, entry: object, identity: str, repository: str
) -> tuple[dict[str, object], Path]:
    if not isinstance(entry, dict) or entry.get("pluginId") != identity:
        raise ValueError(f"Native Codex inventory has no valid {identity} entry")
    name, marketplace, version = (
        entry.get("name"),
        entry.get("marketplaceName"),
        entry.get("version"),
    )
    plugin, _, expected_marketplace = identity.partition("@")
    if (
        not all(
            isinstance(value, str) and value for value in (name, marketplace, version)
        )
        or name != plugin
        or marketplace != expected_marketplace
    ):
        raise ValueError(f"Native Codex inventory has an incomplete {identity} entry")
    payload_source = entry.get("source")
    if not isinstance(payload_source, dict) or not payload_source:
        raise ValueError(f"Native Codex inventory has no source for {identity}")
    source = entry.get("marketplaceSource")
    expected = hook_trust.github_repository(repository)
    actual = (
        hook_trust.github_repository(source.get("source"))
        if isinstance(source, dict)
        else None
    )
    if (
        not expected
        or not isinstance(source, dict)
        or source.get("sourceType") != "git"
        or actual != expected
    ):
        raise ValueError(f"Native Codex inventory publisher differs for {identity}")
    root = cache_path(temporary_home, identity, version)
    if not required_plugins.readable_payload(root, HOST, name):
        raise ValueError(f"Native Codex payload is unreadable for {identity}")
    manifest = json.loads(
        (root / HOST.manifest_directory / "plugin.json").read_text(encoding="utf-8-sig")
    )
    if not isinstance(manifest, dict) or manifest.get("version") != version:
        raise ValueError(f"Native Codex payload version differs for {identity}")
    inspect_tree(root, f"native Codex payload for {identity}")
    return entry, root


def transfer_payload(source: Path, destination: Path) -> None:
    home = HOST.configuration_root
    for ancestor in (
        home,
        home / "plugins",
        home / "plugins" / "cache",
        destination.parents[2],
        destination.parent,
    ):
        if ancestor.exists():
            reject_reparse_or_hardlink(
                ancestor, "Codex cache directory", directory=True
            )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_dir():
            inspect_tree(destination, "existing Codex payload")
        else:
            reject_reparse_or_hardlink(destination, "existing Codex cache entry")
            raise ValueError(
                f"Codex cache destination is not a directory: {destination}"
            )
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent)
    )
    try:
        staging.rmdir()
        shutil.copytree(source, staging)
        inspect_tree(staging, "staged Codex payload")
        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def native_step(command: list[str], project: Path, environment: dict[str, str]) -> str:
    result = process.run(
        command,
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=INSTALL_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit {result.returncode}"
        )
        raise ValueError(f"{' '.join(command[:4])}: {detail}")
    return result.stdout


def acquire_cache(
    project: Path, identities: tuple[str, ...]
) -> list[dict[str, object]]:
    project = exact_project(project)
    sources = selection_sources(project, identities)
    if not identities:
        return []
    with tempfile.TemporaryDirectory(prefix="tj-agents-codex-cache-") as directory:
        temporary_home = Path(directory)
        environment = dict(os.environ, CODEX_HOME=str(temporary_home))
        for marketplace, repository in sorted(sources.items()):
            command = [
                "codex",
                "plugin",
                "marketplace",
                "add",
                repository,
                "--ref",
                "main",
            ]
            if marketplace == required_plugins.MARKETPLACE_NAME:
                for sparse in required_plugins.MARKETPLACE_SPARSE_PATHS:
                    command.extend(("--sparse", sparse))
            native_step(command, project, environment)
        for identity in identities:
            native_step(
                ["codex", "plugin", "add", identity, "--json"], project, environment
            )
        inventory = json.loads(
            native_step(["codex", "plugin", "list", "--json"], project, environment)
        )
        entries = inventory.get("installed") if isinstance(inventory, dict) else None
        if not isinstance(entries, list):
            raise ValueError("Native Codex plugin inventory is invalid")
        received = []
        for identity in identities:
            entry, source = entry_root(
                temporary_home,
                next(
                    (
                        entry
                        for entry in entries
                        if isinstance(entry, dict) and entry.get("pluginId") == identity
                    ),
                    None,
                ),
                identity,
                sources[identity.rpartition("@")[2]],
            )
            destination = cache_path(
                HOST.configuration_root,
                identity,
                entry["version"],
            )
            transfer_payload(source, destination)
            received.append(entry)
    return received


def refresh(
    report: Any, marketplaces: tuple[str, ...], current: Callable[[str, str], bool]
) -> None:
    project = required_plugins.project_root(Path.cwd())
    selected = selected_identities(marketplaces)
    stale = [
        identity
        for identity in selected
        if not current(identity.rpartition("@")[2], identity.rpartition("@")[0])
    ]
    if not stale:
        if selected:
            print(f"{HOST}: {len(selected)} plugin(s) already at their build")
        return
    try:
        rows = installer_boundary.acquire_codex_cache(project, tuple(stale))
    except installer_boundary.InstallerBoundaryError as error:
        report.fail(HOST, "cache acquisition", str(error))
        return
    catalog.remember(rows, verified_install=True)
    received = {
        row.get("pluginId")
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("pluginId"), str)
    }
    for identity in stale:
        if identity not in received:
            report.fail(HOST, identity, "native cache acquisition did not converge")


def reload_open_sessions(report: Any) -> None:
    try:
        with launch.daemon(INSTALL_TIMEOUT_SECONDS) as server:
            projects = {
                required_plugins.project_root(directory)
                for directory in loaded_directories(server)
            }
            completed = []
            state = jobs.state_directory()
            for project in sorted(projects):
                plugin_activation.begin(state, HOST, project)
                try:
                    update_project_standards(project)
                except (
                    OSError,
                    ValueError,
                    required_plugins.RequiredPluginsError,
                    standards.StandardsError,
                ) as error:
                    report.fail(HOST, "repository standards", str(error))
                else:
                    completed.append(project)
            server.request("config/batchWrite", {"edits": [], "reloadUserConfig": True})
            for project in completed:
                plugin_activation.complete(state, HOST, project)
    except (OSError, RuntimeError) as error:
        report.fail(HOST, "open sessions", str(error))


def restart(report: Any, managed: bool) -> None:
    try:
        sessions = launch.liveness(strict=True)
    except LaunchError as error:
        report.fail(HOST, "open sessions", str(error))
        return
    if sessions:
        reload_open_sessions(report)
        if not report.failures:
            print("Open Codex sessions reloaded updated plugins in place.")
    else:
        print("No loaded Codex sessions need activation.")
