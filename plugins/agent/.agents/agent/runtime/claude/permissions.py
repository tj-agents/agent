"""Configure Claude permission defaults and native rules (Python 3.9+)."""

from __future__ import annotations

import json
from pathlib import Path

DENY = tuple(f"{tool}({program} auth {command} *)" for tool in ("Bash", "PowerShell")
             for program in ("gh", "gh.exe") for command in ("login", "refresh"))


def deny(permissions: dict) -> None:
    rules = permissions.setdefault("deny", [])
    if not isinstance(rules, list):
        raise ValueError("Claude permissions.deny must be a list")
    rules.extend(rule for rule in DENY if rule not in rules)


MODES = {"bypass": "bypassPermissions", "standard": "default"}


def settings(text: str, mode: str, *, project: bool = False) -> str:
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
        permissions["defaultMode"] = MODES[mode]
    if not project:
        deny(permissions)
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def plan(path: Path, mode: str, *, applying: bool = False) -> dict[Path, str]:
    if path.is_symlink() or path.exists() and not path.is_file():
        raise ValueError(f"Native settings must be regular files: {path}")
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    updated = settings(text, mode)
    return {path: updated} if updated != text else {}
