#!/usr/bin/env python3
"""Report agent behavior stored outside repository declarations."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat

try:
    import tomllib
except ImportError:
    tomllib = None


HOST_MARKETPLACES = {"openai-bundled", "openai-primary-runtime"}
BEHAVIOR = {
    "enabledPlugins": "plugins", "extraKnownMarketplaces": "marketplaces",
    "plugins": "plugins", "plugin": "plugins", "marketplaces": "marketplaces",
    "marketplace": "marketplaces", "hooks": "hooks", "hook": "hooks",
    "agents": "agents", "agent": "agents", "notify": "notify",
    "model_instructions_file": "model_instructions_file",
}
LOOSE_DIRECTORIES = (
    ".codex/agents", ".codex/skills", ".codex/hooks",
    ".claude/agents", ".claude/skills", ".claude/hooks",
)


def _finding(code: str, path: Path, setting: str) -> dict[str, str]:
    return {"code": code, "path": str(path), "setting": setting}


def _linked(path: Path) -> bool:
    try:
        details = os.lstat(path)
    except (OSError, ValueError):
        return os.path.islink(path)
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _read(path: Path, kind: str, findings: list[dict[str, str]]):
    for candidate in (path.parent.parent, path.parent, path):
        if _linked(candidate):
            findings.append(_finding("linked_path", candidate, kind))
            return None
    try:
        contents = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        findings.append(_finding("unreadable_config", path, kind))
        return None
    try:
        data = json.loads(contents) if kind == "json" else tomllib.loads(contents)
    except (json.JSONDecodeError, ValueError, RecursionError):
        findings.append(_finding("invalid_config", path, kind))
        return None
    if not isinstance(data, dict):
        findings.append(_finding("invalid_config", path, kind))
        return None
    return data


def _scan_settings(data: dict, path: Path, findings: list[dict[str, str]], scope: str,
                   codex: bool = False, prefix: str = ""):
    for key, value in data.items():
        setting = BEHAVIOR.get(key)
        if setting is None or not value:
            continue
        if codex and setting == "marketplaces" and isinstance(value, dict):
            if all(str(name) in HOST_MARKETPLACES for name in value):
                continue
        elif codex and setting == "plugins" and isinstance(value, dict):
            if all(str(name).rsplit("@", 1)[-1] in HOST_MARKETPLACES for name in value):
                continue
        elif codex and setting == "hooks" and isinstance(value, dict):
            if all(name == "state" for name in value):
                continue
        findings.append(_finding(f"{scope}_behavior", path, prefix + setting))


def _toml_unavailable(path: Path, findings: list[dict[str, str]]) -> bool:
    if tomllib is not None:
        return False
    if path.exists() or path.is_symlink():
        findings.append(_finding("python_3_11_required", path, "toml"))
    return True


def _scan_codex(path: Path, findings: list[dict[str, str]], scope: str):
    if _toml_unavailable(path, findings):
        return
    data = _read(path, "toml", findings)
    if data is None:
        return
    _scan_settings(data, path, findings, scope, codex=True)
    profiles = data.get("profiles")
    if profiles is not None and not isinstance(profiles, dict):
        findings.append(_finding("invalid_config", path, "profiles"))
    elif isinstance(profiles, dict):
        for name, profile in profiles.items():
            if isinstance(profile, dict):
                _scan_settings(profile, path, findings, scope, codex=True, prefix=f"profiles.{name}.")
            else:
                findings.append(_finding("invalid_config", path, f"profiles.{name}"))


def _scan_loose(directory: Path, findings: list[dict[str, str]], scope: str):
    for candidate in (directory.parent, directory):
        if _linked(candidate):
            findings.append(_finding("linked_path", candidate, "directory"))
            return
    try:
        children = sorted(directory.iterdir())
    except FileNotFoundError:
        return
    except OSError:
        findings.append(_finding("unreadable_directory", directory, "directory"))
        return
    for child in children:
        if directory.parent.name == ".codex" and directory.name == "skills" and child.name == ".system":
            continue
        findings.append(_finding("linked_path" if _linked(child) else f"{scope}_loose", child, directory.name))


def _local_source(value) -> bool:
    if isinstance(value, dict):
        kind = next((str(value[key]).lower() for key in ("source_type", "type", "kind") if key in value), "")
        if kind in {"local", "path", "directory", "filesystem", "file"}:
            return True
        for key in ("source", "url", "repository"):
            if key in value and _local_source(value[key]):
                return True
        source_marker = value.get("source")
        if kind in {"github", "git", "http", "https"} or (
            isinstance(source_marker, str) and source_marker in {"github", "git", "http", "https"}
        ):
            return False
        return any(_local_source(value[key]) for key in ("path", "location") if key in value)
    if not isinstance(value, str):
        return False
    return (value.lower() in {"local", "path", "directory", "filesystem", "file"}
            or value.lower().startswith("file:") or value.startswith(("/", "./", "../", ".\\", "..\\", "~"))
            or bool(re.match(r"^[A-Za-z]:[\\/]", value)) or value.startswith("\\\\"))


def _scan_repository_codex(path: Path, findings: list[dict[str, str]]):
    if _toml_unavailable(path, findings):
        return
    data = _read(path, "toml", findings)
    if data is None:
        return
    tables = [("", data)]
    profiles = data.get("profiles")
    if profiles is not None and not isinstance(profiles, dict):
        findings.append(_finding("invalid_config", path, "profiles"))
    elif isinstance(profiles, dict):
        for name, value in profiles.items():
            if isinstance(value, dict):
                tables.append((f"profiles.{name}.", value))
            else:
                findings.append(_finding("invalid_config", path, f"profiles.{name}"))
    for prefix, table in tables:
        for key in ("marketplaces", "marketplace"):
            marketplaces = table.get(key)
            if isinstance(marketplaces, dict):
                for source in marketplaces.values():
                    try:
                        local = _local_source(source)
                    except RecursionError:
                        findings.append(_finding("invalid_config", path, prefix + "marketplaces"))
                        continue
                    if local:
                        findings.append(_finding("repository_local_source", path, prefix + "marketplaces"))


def _scan_repository_claude(path: Path, findings: list[dict[str, str]]):
    data = _read(path, "json", findings)
    if data is None:
        return
    sources = data.get("extraKnownMarketplaces")
    if isinstance(sources, dict):
        for source in sources.values():
            try:
                local = _local_source(source)
            except RecursionError:
                findings.append(_finding("invalid_config", path, "extraKnownMarketplaces"))
                continue
            if local:
                findings.append(_finding("repository_local_source", path, "extraKnownMarketplaces"))


def inspect(home: Path, repositories=()) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if _linked(home):
        findings.append(_finding("linked_path", home, "home"))
    elif not home.is_dir():
        findings.append(_finding("missing_home", home, "home"))
    else:
        for path in (home / ".claude" / "settings.json", home / ".claude" / "settings.local.json"):
            data = _read(path, "json", findings)
            if data is not None:
                _scan_settings(data, path, findings, "user")
        _scan_codex(home / ".codex" / "config.toml", findings, "user")
        for relative in LOOSE_DIRECTORIES:
            _scan_loose(home / relative, findings, "user")
    for repository in repositories:
        repository = Path(repository)
        if _linked(repository):
            findings.append(_finding("linked_path", repository, "repository"))
            continue
        if not repository.is_dir():
            findings.append(_finding("missing_repository", repository, "repository"))
            continue
        _scan_loose(repository / ".codex" / "agents", findings, "repository")
        _scan_repository_codex(repository / ".codex" / "config.toml", findings)
        for name in ("settings.json", "settings.local.json"):
            _scan_repository_claude(repository / ".claude" / name, findings)
    return findings


def _render(finding: dict[str, str]) -> str:
    detail = "Python 3.11+ required for TOML inspection" if finding["code"] == "python_3_11_required" else finding["code"].replace("_", " ")
    return f'{finding["path"]}: {detail} ({finding["setting"]})'


def audit(home: Path) -> list[str]:
    return [_render(finding) for finding in inspect(home)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--repository", type=Path, action="append", default=[])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    findings = inspect(args.home, args.repository)
    if args.json:
        print(json.dumps({"status": "drift" if findings else "clean", "findings": findings}, indent=2))
    else:
        for finding in findings:
            print(_render(finding))
        if not findings:
            print("No user-scope agent behavior found.")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
