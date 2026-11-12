"""Project Core's permission preference into native host settings (Python 3.11+)."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
import tomllib


sys.path.insert(0, str(Path(__file__).resolve().parent))


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
