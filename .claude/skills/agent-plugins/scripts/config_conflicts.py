"""Read conflicted declarations as structured inputs without changing Git state."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile


MISSING = object()


def merge(base, ours, theirs, label: str):
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if all(isinstance(value, dict) for value in (base, ours, theirs)):
        result = {}
        for key in sorted(base.keys() | ours.keys() | theirs.keys()):
            value = merge(base.get(key, MISSING), ours.get(key, MISSING), theirs.get(key, MISSING),
                          f"{label}.{key}")
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


def unmanaged_json(text: str) -> dict:
    document = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError("Configuration must be a JSON object")
    document.pop("extraKnownMarketplaces", None)
    document.pop("enabledPlugins", None)
    # Approval declarations are generated from repository-harness.json too.
    permissions = document.get("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError("Configuration permissions must be an object")
    permissions.pop("allow", None)
    document["permissions"] = permissions
    return document


def unmanaged_toml(text: str) -> str:
    # Use the generator's existing ownership boundary, including older tables
    # that predate its marked block.
    import repo_config

    return repo_config.unmanaged_codex(text)


def read_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not any(line.startswith("<<<<<<< ") for line in text.splitlines()):
        return text
    relative = path.parent.name + "/" + path.name
    if relative not in {".agents/plugins.json", ".claude/settings.json", ".codex/config.toml"}:
        return text
    root = path.parent.parent.resolve()
    # A real unmerged index is required. Committed markers or arbitrary invalid
    # configuration are never treated as permission to reconstruct user data.
    stages = subprocess.run(["git", "--literal-pathspecs", "ls-files", "-u", "--", relative],
                            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=10)
    if stages.returncode or {line.split("\t", 1)[0].rsplit(" ", 1)[-1]
                             for line in stages.stdout.splitlines()} != {"1", "2", "3"}:
        raise ValueError(f"{path}: configuration markers have no complete Git merge stages")
    result = subprocess.run(["git", "show", ":1:" + relative], cwd=root, capture_output=True,
                            text=True, encoding="utf-8", timeout=10, check=True)
    base = result.stdout
    ours, theirs = variants(text)
    if relative == ".agents/plugins.json":
        documents = [json.loads(value) for value in (base, ours, theirs)]
        import plugin_selection

        available = {}
        for document in documents:
            available.update(document["marketplaces"])
        for document in documents:
            used = {identity.rsplit("@", 1)[1] for identity in document["plugins"]}
            plugin_selection.validate({**document, "marketplaces": {
                key: value for key, value in available.items() if key in used}})
        selections = [set(document.pop("plugins")) for document in documents]
        marketplaces = [document.pop("marketplaces") for document in documents]
        merged = merge(*documents, relative)
        original, left, right = selections
        merged["plugins"] = sorted((original & left & right) | (left - original) | (right - original))
        # A plugin removed on either side also retires its now-unused marketplace.
        used = {identity.rsplit("@", 1)[1] for identity in merged["plugins"]}
        merged["marketplaces"] = {}
        for key in sorted(used):
            original_source, left_source, right_source = [value.get(key, MISSING) for value in marketplaces]
            merged["marketplaces"][key] = merge(
                original_source,
                original_source if left_source is MISSING else left_source,
                original_source if right_source is MISSING else right_source,
                f"{relative}.marketplaces.{key}")
        plugin_selection.validate(merged)
        return json.dumps(merged, indent=2) + "\n"
    if relative == ".claude/settings.json":
        return json.dumps(merge(*(unmanaged_json(value) for value in (base, ours, theirs)), relative),
                          indent=2) + "\n"
    prefixes = [unmanaged_toml(value) for value in (base, ours, theirs)]
    with tempfile.TemporaryDirectory(prefix="agent-config-merge-") as directory:
        files = [Path(directory) / name for name in ("base", "ours", "theirs")]
        for file, value in zip(files, prefixes):
            file.write_text(value, encoding="utf-8")
        result = subprocess.run(["git", "merge-file", "-p", str(files[1]), str(files[0]), str(files[2])],
                                cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=10)
    if result.returncode:
        raise ValueError(f"{path}: conflicting unmanaged TOML requires resolution")
    import tomllib

    tomllib.loads(result.stdout)
    return result.stdout
