"""Claude's native permission rules that Agent installs (Python 3.9+)."""

from __future__ import annotations

DENY = tuple(f"{tool}({program} auth {command} *)" for tool in ("Bash", "PowerShell")
             for program in ("gh", "gh.exe") for command in ("login", "refresh"))


def deny(permissions: dict) -> None:
    rules = permissions.setdefault("deny", [])
    if not isinstance(rules, list):
        raise ValueError("Claude permissions.deny must be a list")
    rules.extend(rule for rule in DENY if rule not in rules)
