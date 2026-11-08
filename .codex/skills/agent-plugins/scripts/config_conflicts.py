"""Read conflicted declarations as structured inputs without changing Git state."""

from __future__ import annotations

import json
import subprocess
import tempfile
import tomllib
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from plugin_runtime import process

MISSING = object()


def marker_lines(text: str) -> Counter[str]:
    return Counter(
        line
        for line in text.splitlines()
        if line.startswith(("<<<<<<<", "|||||||", "=======", ">>>>>>>"))
    )


def merge(base: Any, ours: Any, theirs: Any, label: str) -> Any:
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if base is MISSING and isinstance(ours, dict) and isinstance(theirs, dict):
        base = {}
    if all(isinstance(value, dict) for value in (base, ours, theirs)):
        result: dict[Any, Any] = {}
        for key in sorted(base.keys() | ours.keys() | theirs.keys()):
            value = merge(
                base.get(key, MISSING),
                ours.get(key, MISSING),
                theirs.get(key, MISSING),
                f"{label}.{key}",
            )
            if value is not MISSING:
                result[key] = value
        return result
    raise ValueError(f"Cannot automatically resolve configuration conflict: {label}")


def variants(text: str) -> tuple[str, str]:
    ours, theirs = [], []
    state = "common"
    count = 0
    for line in text.splitlines(keepends=True):
        if line.startswith("<<<<<<< ") and state == "common":
            state = "ours"
            count += 1
        elif line.startswith("||||||| ") and state == "ours":
            state = "base"
        elif line.rstrip("\r\n") == "=======" and state in {"ours", "base"}:
            state = "theirs"
        elif line.startswith(">>>>>>> ") and state == "theirs":
            state = "common"
        elif line.startswith(("<<<<<<<", "|||||||", "=======", ">>>>>>>")):
            raise ValueError("Malformed configuration conflict markers")
        else:
            if state in {"common", "ours"}:
                ours.append(line)
            if state in {"common", "theirs"}:
                theirs.append(line)
    if state != "common" or not count:
        raise ValueError("Incomplete configuration conflict markers")
    return "".join(ours), "".join(theirs)


def unmanaged_json(text: str) -> dict[str, Any]:
    document: Any = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError("Configuration must be a JSON object")
    document.pop("extraKnownMarketplaces", None)
    document.pop("enabledPlugins", None)
    # Keep approvals visible to the planner's ownership validation. Discarding
    # them here would silently erase user edits made before or after a conflict.
    permissions = document.get("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError("Configuration permissions must be an object")
    document["permissions"] = permissions
    return document


def read_text(path: Path, unmanaged: Callable[[str], str] | None = None) -> str:
    text = path.read_text(encoding="utf-8")
    if not any(line.startswith("<<<<<<< ") for line in text.splitlines()):
        return text
    relative = path.parent.name + "/" + path.name
    if relative not in {
        ".agents/plugins.json",
        ".claude/settings.json",
        ".codex/config.toml",
    }:
        return text
    root = path.parent.parent.resolve()
    valid_toml = False
    if path.suffix == ".toml":
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            pass
        else:
            valid_toml = True
    # A real unmerged index is required. Committed markers or arbitrary invalid
    # configuration are never treated as permission to reconstruct user data.
    stages = process.run(
        ["git", "--literal-pathspecs", "ls-files", "-u", "--", relative],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    if stages.returncode or {
        line.split("\t", 1)[0].rsplit(" ", 1)[-1] for line in stages.stdout.splitlines()
    } != {"1", "2", "3"}:
        # Literal examples are valid TOML too, but actual conflicts inside a
        # multiline string remain parseable. Authenticate Git state first.
        if valid_toml:
            return text
        raise ValueError(
            f"{path}: configuration markers have no complete Git merge stages"
        )
    result: subprocess.CompletedProcess[str] = process.run(
        ["git", "show", ":1:" + relative],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=True,
    )
    base = result.stdout
    if valid_toml:
        # An unstaged manual resolution retains Git's unmerged stages. Literal
        # marker lines already shared by every stage must not reopen that work.
        markers = marker_lines(text)
        staged = [base]
        for stage in (2, 3):
            staged.append(
                process.run(
                    ["git", "show", f":{stage}:" + relative],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=10,
                    check=True,
                ).stdout
            )
        if all(markers <= marker_lines(value) for value in staged):
            return text
    ours, theirs = variants(text)
    if relative == ".agents/plugins.json":
        documents: list[dict[str, Any]] = []
        for value in (base, ours, theirs):
            document: Any = json.loads(value)
            if not isinstance(document, dict):
                raise ValueError("Plugin selection must be a JSON object")
            marketplaces = document.get("marketplaces")
            plugins = document.get("plugins")
            if (
                not isinstance(marketplaces, dict)
                or not isinstance(plugins, list)
                or any(not isinstance(identity, str) for identity in plugins)
            ):
                raise ValueError(
                    "Plugin selection must contain marketplace and plugin collections"
                )
            documents.append(document)
        selections = [set(document.pop("plugins")) for document in documents]
        marketplaces = [document.pop("marketplaces") for document in documents]
        merged = merge(documents[0], documents[1], documents[2], relative)
        original, left, right = selections
        merged["plugins"] = sorted(
            (original & left & right) | (left - original) | (right - original)
        )
        # A plugin removed on either side also retires its now-unused marketplace.
        used = {identity.rsplit("@", 1)[-1] for identity in merged["plugins"]}
        merged_marketplaces: dict[str, Any] = {}
        for key in sorted(used):
            original_source, left_source, right_source = [
                value.get(key, MISSING) for value in marketplaces
            ]
            merged_marketplaces[key] = merge(
                original_source,
                original_source if left_source is MISSING else left_source,
                original_source if right_source is MISSING else right_source,
                f"{relative}.marketplaces.{key}",
            )
        merged["marketplaces"] = merged_marketplaces
        return json.dumps(merged, indent=2) + "\n"
    if relative == ".claude/settings.json":
        documents = [unmanaged_json(value) for value in (base, ours, theirs)]
        return (
            json.dumps(
                merge(documents[0], documents[1], documents[2], relative),
                indent=2,
            )
            + "\n"
        )
    if unmanaged is None:
        raise ValueError(
            "An unmanaged TOML reader is required to reconcile Codex settings"
        )
    prefixes = [unmanaged(value) for value in (base, ours, theirs)]
    with tempfile.TemporaryDirectory(prefix="agent-config-merge-") as directory:
        files = [Path(directory) / name for name in ("base", "ours", "theirs")]
        for file, value in zip(files, prefixes, strict=True):
            file.write_text(value, encoding="utf-8")
        result = process.run(
            ["git", "merge-file", "-p", str(files[1]), str(files[0]), str(files[2])],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    if result.returncode:
        raise ValueError(f"{path}: conflicting unmanaged TOML requires resolution")
    tomllib.loads(result.stdout)
    return result.stdout
