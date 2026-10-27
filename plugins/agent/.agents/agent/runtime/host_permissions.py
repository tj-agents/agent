"""Project Core's permission preference into native host settings (Python 3.11+)."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))

from host import Host  # noqa: E402

CODEX_KEYS = frozenset({"approval_policy", "default_permissions", "sandbox_mode", "sandbox_workspace_write"})
MODES = {
    "bypass": ("bypassPermissions", "never", ":danger-full-access"),
    "standard": ("default", "on-request", ":workspace"),
}


def permissions(root: Path) -> str:
    directory = Path(__file__).resolve().parent
    candidates = (directory / "repository_configuration.py",
                  directory.parents[1] / "workflows" / "repository_configuration.py")
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("permissions_repository_configuration", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.permissions(root)
    raise ValueError("The shipped repository_configuration.py was not found")


def claude_settings(text: str, mode: str, *, project: bool = False) -> str:
    if mode not in MODES:
        raise ValueError("Permission mode must be bypass or standard")
    settings = json.loads(text) if text.strip() else {}
    if not isinstance(settings, dict):
        raise ValueError("Claude settings must be an object")
    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError("Claude permissions must be an object")
    if mode == "bypass" and permissions.get("disableBypassPermissionsMode") == "disable":
        raise ValueError("Claude bypass permissions is disabled; resolve the conflicting native policy first")
    if project and mode == "bypass":
        permissions.pop("defaultMode", None)
    else:
        permissions["defaultMode"] = MODES[mode][0]
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def codex_settings(text: str, mode: str) -> str:
    if mode not in MODES:
        raise ValueError("Permission mode must be bypass or standard")
    before = tomllib.loads(text)
    expected = {key: copy.deepcopy(value) for key, value in before.items() if key not in CODEX_KEYS}
    desired = {"approval_policy": MODES[mode][1], "default_permissions": MODES[mode][2]}
    expected.update(desired)
    if before == expected:
        return text
    kept = []
    statement = ""
    root = True
    remove_table = False
    for line in text.splitlines(keepends=True):
        statement += line
        try:
            value = tomllib.loads(statement)
        except tomllib.TOMLDecodeError:
            continue
        if statement.lstrip().startswith("["):
            root = False
            remove_table = next(iter(value), None) in CODEX_KEYS
            if not remove_table:
                kept.append(statement)
        elif not remove_table and not (root and set(value) & CODEX_KEYS):
            kept.append(statement)
        statement = ""
    if statement:
        raise ValueError("Cannot identify complete Codex settings statements")
    newline = "\r\n" if "\r\n" in text else "\n"
    prefix = newline.join(f"{key} = {json.dumps(value)}" for key, value in desired.items())
    result = prefix + newline + newline + "".join(kept).lstrip("\r\n")
    if tomllib.loads(result) != expected:
        raise ValueError("Cannot update Codex permission settings without changing unrelated configuration")
    return result


def desktop_executables() -> list[Path]:
    if os.name == "nt":
        command = (
            "$ErrorActionPreference = 'Stop'; "
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
            "Get-CimInstance Win32_Process -Filter \"Name = 'Codex.exe'\" | "
            "Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", check=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        rows = json.loads(result.stdout) if result.stdout.strip() else []
        rows = [rows] if isinstance(rows, dict) else rows
        if any(not row.get("ExecutablePath") for row in rows):
            raise ValueError("Cannot verify that Codex Desktop is closed: a process path is unavailable")
        return [Path(row["ExecutablePath"]) for row in rows]
    if sys.platform == "linux":
        paths = []
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                if (process / "comm").read_text(encoding="utf-8", errors="replace").strip().casefold() != Host.CODEX.value:
                    continue
                paths.append((process / "exe").resolve(strict=True))
            except FileNotFoundError:
                continue
        return paths
    result = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True,
                            encoding="utf-8", check=True, timeout=15)
    return [Path(line.strip()) for line in result.stdout.splitlines()
            if Path(line.strip()).name.casefold() == Host.CODEX.value]


def desktop_running(executables=None) -> bool:
    try:
        paths = desktop_executables() if executables is None else executables
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("Cannot verify that Codex Desktop is closed") from error
    return any((path.parent / "resources/app.asar").is_file()
               or path.parent.name == "MacOS" and (path.parent.parent / "Resources/app.asar").is_file()
               for path in paths)


def codex_app_settings(text: str) -> str:
    settings = json.loads(text)
    if not isinstance(settings, dict):
        raise ValueError("Codex app state must be an object")
    if "electron-persisted-atom-state" not in settings:
        return text
    atoms = settings["electron-persisted-atom-state"]
    if not isinstance(atoms, dict):
        raise ValueError("Codex app atom state must be an object")
    if "agent-mode-by-host-id" not in atoms:
        return text
    modes = atoms["agent-mode-by-host-id"]
    if not isinstance(modes, dict):
        raise ValueError("Codex app host modes must be an object")
    if "local" not in modes:
        return text
    del modes["local"]
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def plan(claude: Path, codex: Path, mode: str) -> dict[Path, str]:
    targets = {}
    for path, render in ((claude, claude_settings), (codex, codex_settings)):
        if path.is_symlink() or path.exists() and not path.is_file():
            raise ValueError(f"Native settings must be regular files: {path}")
        text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
        updated = render(text, mode)
        if updated != text:
            targets[path] = updated
    desktop = codex.parent / ".codex-global-state.json"
    if desktop.is_symlink() or desktop.exists() and not desktop.is_file():
        raise ValueError(f"Native settings must be regular files: {desktop}")
    if desktop.is_file():
        text = desktop.read_text(encoding="utf-8-sig")
        updated = codex_app_settings(text)
        if updated != text:
            targets[desktop] = updated
    return targets
