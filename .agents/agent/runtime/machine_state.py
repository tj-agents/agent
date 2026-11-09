"""Machine drift from every cheap install check, fingerprinted by what they read."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from claude import installed as claude_installed

MARKETPLACES = ("tj-agents", "agents")
RUNTIME = Path(__file__).resolve().parent
FINGERPRINT = "machine-fingerprint.json"


def packaged(relative: str) -> Path:
    agent = claude_installed.installs("tj-agents").get("agent")
    script = agent[1] / ".agents" / "agent" / "runtime" / relative if agent else None
    return script if script is not None and script.is_file() else RUNTIME / relative


def plugin_builds() -> dict[str, list[str]]:
    builds = {}
    for marketplace in MARKETPLACES:
        pins = claude_installed.pins(marketplace)
        for plugin, (revision, root) in claude_installed.installs(marketplace).items():
            builds[f"{plugin}@{marketplace}"] = [
                pins.get(plugin, ""),
                revision,
                str(root),
            ]
    return builds


def modified(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def fingerprint(paths: Iterable[Path]) -> dict[str, object]:
    files = {str(path): modified(path) for path in paths}
    return {"plugins": plugin_builds(), "files": files}


def current(state: Path) -> bool:
    try:
        recorded = json.loads((state / FINGERPRINT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    files = recorded.get("files") if isinstance(recorded, dict) else None
    if not isinstance(files, dict):
        return False
    return bool(recorded == fingerprint(Path(name) for name in files))


def drift(state: Path) -> dict[str, list[str]]:
    # Deferred: install takes ~0.8 s to import, which a prompt with a current
    # fingerprint must never pay.
    import install

    context = install.machine_context()
    context.root = packaged("install.py").parents[3]
    steps = [
        (step, step.watch)
        for step in install.STEPS
        if step.watch is not None and not step.skip(context)
    ]
    watched: list[Path] = []
    found = {}
    for step, watch in steps:
        try:
            watched += watch(context)
        except Exception as error:  # noqa: BLE001
            found[step.name] = [str(error)]
    seen = fingerprint(watched)
    for step, _ in steps:
        if step.name in found:
            continue
        try:
            pending = step.check(context)
        except Exception as error:  # noqa: BLE001
            pending = [str(error)]
        if pending:
            found[step.name] = pending
    marker = state / FINGERPRINT
    if found:
        marker.unlink(missing_ok=True)
    else:
        state.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(seen), encoding="utf-8")
    return found
