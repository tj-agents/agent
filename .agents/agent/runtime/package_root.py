"""Locate an Agent package root without consulting machine-local installations."""

from __future__ import annotations

import json
from pathlib import Path
import re


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _staged_agent_source(root: Path) -> bool:
    """Accept Core's explicit, receipted build input and nothing broader."""
    try:
        receipt = json.loads((root / "source.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(receipt, dict) or receipt.get("repository") != "tj-agents/agent":
        return False
    revision, digest, exports, files = (receipt.get("revision"), receipt.get("sha256"), receipt.get("exports"),
                                        receipt.get("files"))
    return (isinstance(revision, str) and bool(revision)
            and isinstance(digest, str) and bool(_SHA256.fullmatch(digest))
            and isinstance(exports, list) and bool(exports) and "pending" not in receipt
            and all(isinstance(item, str) and item.startswith(".agents/") for item in exports)
            and isinstance(files, dict) and bool(files))


def is_package_root(root: Path) -> bool:
    if not (root / ".agents" / "agent" / "runtime").is_dir():
        return False
    native = any((root / marker).exists() for marker in (
        ".git", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json",
    ))
    return native or _staged_agent_source(root)


def find_package_root(start: Path) -> Path:
    for root in (start, *start.parents):
        if is_package_root(root):
            return root
    raise RuntimeError(f"The Agent package root was not found relative to {start}.")
