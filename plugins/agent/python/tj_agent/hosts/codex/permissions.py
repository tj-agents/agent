"""Codex's native execpolicy rules that Agent installs (Python 3.9+)."""

from __future__ import annotations

from pathlib import Path

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
