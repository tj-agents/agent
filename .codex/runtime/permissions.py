"""Configure Codex permission defaults and native rules (Python 3.11+)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

RULES = "tj-agents-credentials.rules"
TEXT = """prefix_rule(
    pattern = [["gh", "gh.exe"], "auth", ["login", "refresh"]],
    decision = "forbidden",
    justification = "Account-wide OAuth scopes reach every organization; ask the human for a fine-grained token for the one call instead.",
    match = [["gh", "auth", "refresh", "-s", "admin:org"], ["gh", "auth", "login"]],
    not_match = [["gh", "auth", "status"], ["gh", "auth", "token"]],
)
"""


def rules_path(config: Path) -> Path:
    return config.parent / "rules" / RULES


CODEX_KEYS = frozenset({"approval_policy", "default_permissions", "sandbox_mode", "sandbox_workspace_write"})
MODES = {"bypass": ("never", ":danger-full-access"), "standard": ("on-request", ":workspace")}


def settings(text: str, mode: str, *, project: bool = False) -> str:
    if mode not in MODES:
        raise ValueError("Permission mode must be bypass or standard")
    before = tomllib.loads(text)
    expected = {key: copy.deepcopy(value) for key, value in before.items() if key not in CODEX_KEYS}
    desired = {"approval_policy": MODES[mode][0], "default_permissions": MODES[mode][1]}
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
                if (process / "comm").read_text(encoding="utf-8", errors="replace").strip().casefold() != "codex":
                    continue
                paths.append((process / "exe").resolve(strict=True))
            except FileNotFoundError:
                continue
        return paths
    result = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True,
                            encoding="utf-8", check=True, timeout=15)
    return [Path(line.strip()) for line in result.stdout.splitlines()
            if Path(line.strip()).name.casefold() == "codex"]


def desktop_running(executables=None) -> bool:
    try:
        paths = desktop_executables() if executables is None else executables
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("Cannot verify that Codex Desktop is closed") from error
    return any((path.parent / "resources/app.asar").is_file()
               or path.parent.name == "MacOS" and (path.parent.parent / "Resources/app.asar").is_file()
               for path in paths)


def app_settings(text: str) -> str:
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


def plan(config: Path, mode: str, *, applying: bool = False) -> dict[Path, str]:
    targets = {}
    rules = rules_path(config)
    desktop = config.parent / ".codex-global-state.json"
    for path in (config, rules, desktop):
        if path.is_symlink() or path.exists() and not path.is_file():
            raise ValueError(f"Native settings must be regular files: {path}")
    text = config.read_text(encoding="utf-8-sig") if config.is_file() else ""
    updated = settings(text, mode)
    if updated != text:
        targets[config] = updated
    if (rules.read_text(encoding="utf-8-sig") if rules.is_file() else None) != TEXT:
        targets[rules] = TEXT
    if desktop.is_file():
        text = desktop.read_text(encoding="utf-8-sig")
        updated = app_settings(text)
        if updated != text:
            targets[desktop] = updated
    if applying and desktop in targets:
        if desktop_running():
            raise ValueError("Close Codex Desktop, run install --step 'native permission defaults' "
                             "from a terminal, then reopen the app to load the configured defaults")
        return plan(config, mode)
    return targets
