"""Diagnose installed admission and the executing hook, never a session-wide snapshot.

SessionStart does not supply the loaded plugin inventory. A hook's package path
identifies only that hook; current native inventory cannot attest to cached skills
or modules elsewhere in a running session. Even a matching Agent root therefore
leaves the session's other loaded versions unverified.
"""

import json
import shutil
from pathlib import Path

import host_plugins
import package_root
import required_plugins
from tj_agent.host import Host

AGENT = "agent@tj-agents"
INVENTORY_SECONDS = 3.0
DETAIL_LIMIT = 5000
EVIDENCE_LIMIT = (
    "Session-wide loaded plugin versions remain unverified: SessionStart provides "
    "no complete loaded-plugin snapshot. Installed inventory is not evidence of "
    "the builds already loaded by this session. Reload plugins (or restart this "
    "session) before relying on updated instructions."
)


def inventory(
    host: Host, project: Path, selected: tuple[str, ...]
) -> tuple[str, list[object]]:
    """Read maintenance inventory with one deadline, including dependency markets."""
    binary = shutil.which(host.value)
    if not binary:
        raise required_plugins.RequiredPluginsError(f"{host} executable is unavailable")
    rows = required_plugins.inventory_rows(
        project, host, binary, identities=(*selected, AGENT), timeout=INVENTORY_SECONDS
    )
    return binary, rows


def build(root: Path, host: Host) -> str:
    """Describe a package on disk without inferring an in-memory version."""
    manifest = root / host.manifest_directory / "plugin.json"
    document = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict) or document.get("name") != "agent":
        raise ValueError(f"Invalid Agent manifest at {manifest}")
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"Agent version is unavailable at {manifest}")
    return f"version {version} at {root}"


def agent_build(
    host: Host, project: Path, rows: list[object], executing_file: Path
) -> str:
    plugins = host_plugins.PLUGINS[host]
    installed = required_plugins.enabled_roots(plugins, project, rows).get(AGENT)
    if installed is None:
        errors = required_plugins.failures(plugins, project, rows).get(AGENT, [])
        reasons = "; ".join(str(error["message"]) for error in errors)
        return (
            "Agent hook freshness is unverified: Agent has no valid enabled "
            "installed build in the current inventory."
            + (f" Native loader errors: {reasons}" if reasons else "")
        )
    executing = package_root.find_package_root(executing_file.resolve().parent)
    loaded_build, installed_build = build(executing, host), build(installed, host)
    if executing.resolve() != installed.resolve():
        return (
            f"STALE Agent hook or different build: executing {loaded_build}; "
            f"currently installed {installed_build}. Reload plugins or restart "
            "this session."
        )
    return (
        f"The executing Agent hook matches the installed root ({loaded_build}). "
        "This identifies the hook package only, not other cached Agent content."
    )


def warning(detail: str) -> str:
    return f"Plugin startup warning: {detail[:DETAIL_LIMIT]}\n{EVIDENCE_LIMIT}"


def check(host: Host, project: Path, *, executing_file: Path | None = None) -> str:
    """Return a non-admitting diagnostic even when installed admission succeeds."""
    project = project.resolve()
    details = []
    try:
        selected = required_plugins.selected(required_plugins.project_root(project))
    except required_plugins.RequiredPluginsError as error:
        selected = ()
        details.append(f"Selected plugin admission is unverified: {error}")
    try:
        binary, rows = inventory(host, project, selected)
    except (OSError, RuntimeError, ValueError) as error:
        details.append(
            f"Installed plugin admission and freshness are unverified: {error}"
        )
        return warning("\n".join(details))
    if not details:
        try:
            required_plugins.check(project, host, binary, entries=rows)
        except required_plugins.RequiredPluginsError as error:
            details.append(f"Selected plugin admission failed: {error}")
        else:
            details.append(
                "The selected plugin closure is available in current "
                "installed inventory."
                if selected
                else "This project has no declared plugin selection to validate."
            )
    try:
        details.append(
            agent_build(host, project, rows, executing_file or Path(__file__))
        )
    except (OSError, RuntimeError, ValueError) as error:
        details.append(f"Executing Agent hook freshness is unverified: {error}")
    return warning("\n".join(details))
