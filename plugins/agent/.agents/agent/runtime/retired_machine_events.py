"""Remove the retired machine-events watcher that older cached Agent builds still install (Python 3.9+)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

RUNTIME_FILES = ("machine_events.py", "event_service.py", "event_watch.py")


def state_directory() -> Path:
    return Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state")


def remove(state_directory: Path, *, windows: bool = os.name == "nt", home: Path | None = None,
           run=subprocess.run) -> list[str]:
    """Repeats on purpose: older cached Agent builds re-create the watcher whenever they run, until they age out."""
    state = str(state_directory.resolve())
    name = "tj-agents-machine-events-" + hashlib.sha256((state.casefold() if windows else state).encode()).hexdigest()[:16]
    removed = []
    options = {"capture_output": True, "text": True, "timeout": 30, "check": False}
    if windows:
        result = run(["schtasks", "/Delete", "/TN", name, "/F"], **options,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            removed.append(f"scheduled task {name}")
    else:
        unit = (home or Path.home()) / ".config" / "systemd" / "user" / f"{name}.service"
        if unit.exists():
            run(["systemctl", "--user", "disable", "--now", unit.name], **options)
            unit.unlink(missing_ok=True)
            run(["systemctl", "--user", "daemon-reload"], **options)
            removed.append(f"systemd user unit {unit.name}")
    runtime = state_directory / "bin" / ".agents" / "agent" / "runtime"
    for file in RUNTIME_FILES:
        path = runtime / file
        if path.is_file():
            path.unlink()
            removed.append(str(path))
    return removed
