"""Remove state that retired Agent machinery left in the agents-state directory."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from tj_cli import process

Runner = Callable[..., subprocess.CompletedProcess[str]]

RETIRED_ENTRIES = (
    "backups",
    "capacity-inputs",
    "claude-standards-sync.json",
    "client-close",
    "close-assessments",
    "close-lifecycle",
    "codex-plugin-bytes.json",
    "continuations",
    "currency",
    "harness-permissions.json.38kuhs2u",
    "install-recovery.lock",
    "launch-check.lock",
    "listeners",
    "machine-events-cursor.json",
    "machine-events-service.json",
    "machine-events-state.json",
    "machine-events-watch.lock",
    "machine-events.json",
    "machine-events.lock",
    "machine-events.log",
    "machine-events.log.1",
    "machine-events.log.2",
    "migrations",
    "native-cli.lock",
    "orphan-reaper-notice.json",
    "plugin-autoupdate.json",
    "plugin-autoupdate.lock",
    "plugin-cache-notice.json",
    "plugin-heads.json",
    "plugin-heads.lock",
    "plugin-pins",
    "publish-update-owner-setup-result.json",
    "published-manifest.json",
    "published-repository.git",
    "recovery-launches",
    "refresh-close",
    "repair-backups",
    "session-close",
    "session-relaunch",
    "tj-agents-continuation-stop",
    "bin/agent_cli.py",
    "bin/cli_client.py",
    "bin/codex_hook_trust.py",
    "bin/codex_websocket.py",
    "bin/launch_check.py",
    "bin/launch_status.py",
    "bin/machine_events.py",
    "bin/plugin_autoupdate.py",
    "bin/register_session.py",
    "bin/reload_plugins.py",
    "bin/required_plugins.py",
    "bin/startup_binding.py",
    "bin/terminal_command.py",
    "bin/.agents/agent-store",
    "bin/.agents/agent/runtime/machine_events.py",
    "bin/.agents/agent/runtime/event_service.py",
    "bin/.agents/agent/runtime/event_watch.py",
)


def _watcher_name(state_directory: Path, windows: bool) -> str:
    state = str(state_directory.resolve())
    digest = hashlib.sha256(
        (state.casefold() if windows else state).encode()
    ).hexdigest()[:16]
    return f"tj-agents-machine-events-{digest}"


def _remove_watcher(
    state_directory: Path, windows: bool, home: Path | None, run: Runner
) -> list[str]:
    name = _watcher_name(state_directory, windows)
    options = {"capture_output": True, "text": True, "timeout": 30, "check": False}
    try:
        if windows:
            result = run(["schtasks", "/Delete", "/TN", name, "/F"], **options)
            return [f"scheduled task {name}"] if result.returncode == 0 else []
        unit = (
            (home or Path.home()) / ".config" / "systemd" / "user" / f"{name}.service"
        )
        if not unit.exists():
            return []
        run(["systemctl", "--user", "disable", "--now", unit.name], **options)
        unit.unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"], **options)
        return [f"systemd user unit {unit.name}"]
    except OSError:
        return []


def remove(
    state_directory: Path,
    *,
    windows: bool = os.name == "nt",
    home: Path | None = None,
    run: Runner = process.run,
) -> list[str]:
    """Repeats on purpose: older cached Agent builds re-create some entries until
    they age out."""
    removed = _remove_watcher(state_directory, windows, home, run)
    for entry in RETIRED_ENTRIES:
        path = state_directory / entry
        if path.is_symlink():
            path.unlink()
            removed.append(str(path))
        elif path.is_dir():
            shutil.rmtree(path)
            removed.append(str(path))
        elif path.is_file():
            path.unlink()
            removed.append(str(path))
    return removed
