"""Pinned installs whose plugins keep machine copies, and the builds they came from."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import required_plugins
from claude import installed as claude_installed

MARKETPLACES = ("tj-agents", "agents")
MARKER = "plugin-copies.json"


@dataclass(frozen=True, slots=True)
class Source:
    identity: str
    revision: str
    root: Path


def sources() -> list[Source]:
    found = []
    for marketplace in MARKETPLACES:
        pins = claude_installed.pins(marketplace)
        installs = sorted(claude_installed.installs(marketplace).items())
        for plugin, (revision, root) in installs:
            if pins.get(plugin) == revision and required_plugins.converges(root):
                found.append(Source(f"{plugin}@{marketplace}", revision, root))
    return found


def converged(state: Path) -> dict[str, str]:
    try:
        document = json.loads((state / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    return {
        identity: revision
        for identity, revision in document.items()
        if isinstance(identity, str) and isinstance(revision, str)
    }


def drift(state: Path) -> list[str]:
    recorded = converged(state)
    found = []
    for source in sources():
        build = recorded.get(source.identity)
        if build != source.revision:
            origin = build[:12] if build else "no build"
            pinned = source.revision[:12]
            found.append(f"{source.identity} copies are from {origin}, pinned {pinned}")
    return found


def record(state: Path, converged_from: Sequence[Source]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    document = {source.identity: source.revision for source in converged_from}
    (state / MARKER).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
