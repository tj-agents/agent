"""The pinned Agent build's packaged runtime scripts."""

from __future__ import annotations

from pathlib import Path

from claude import installed as claude_installed

RUNTIME = Path(__file__).resolve().parent


def packaged(relative: str) -> Path:
    agent = claude_installed.pinned_install("tj-agents", "agent")
    script = agent / ".agents" / "agent" / "runtime" / relative if agent else None
    return script if script is not None and script.is_file() else RUNTIME / relative
