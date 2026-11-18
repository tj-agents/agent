#!/usr/bin/env python3
"""Project verified plugin permissions into repository-local files."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import harness_permissions
import lock_io
import plugin_selection
import repo_config

if sys.version_info < (3, 12):
    raise SystemExit("Repository permission projection requires Python 3.12 or newer")

CLAUDE_SETTINGS = Path(".claude/settings.local.json")
CODEX_RULES = Path(".codex/rules/repository-permissions.rules")
STATE = Path(".agents/repository-permissions.local.json")
RULES_HEADER = "# Generated from verified selected plugin harness permissions."
JsonObject = dict[str, Any]


def _redirected(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _require_direct_path(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            redirected = _redirected(candidate)
        except FileNotFoundError:
            continue
        if redirected:
            raise lock_io.LockError(
                f"Refusing redirected permission target: {candidate}"
            )


def _verified_root(identity: str, root: Path) -> tuple[Path, JsonObject]:
    candidate = root.absolute()
    for parent in (candidate, *candidate.parents):
        try:
            if _redirected(parent):
                raise lock_io.LockError(
                    f"{identity}: verified plugin root is redirected: {parent}"
                )
        except OSError as error:
            raise lock_io.LockError(
                f"{identity}: verified plugin root cannot be inspected: {error}"
            ) from error
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise lock_io.LockError(
            f"{identity}: verified plugin root is unavailable: {error}"
        ) from error
    if resolved != candidate or not resolved.is_dir():
        raise lock_io.LockError(
            f"{identity}: verified plugin root is not an exact directory"
        )
    harness = resolved / "harness.json"
    if _redirected(harness) or not harness.is_file():
        raise lock_io.LockError(f"{identity}: verified plugin harness is unavailable")
    document = lock_io.load_json(harness)
    plugin = document.get("plugin")
    marketplace, separator, name = (
        plugin.partition("/") if isinstance(plugin, str) else ("", "", "")
    )
    if not separator or f"{name}@{marketplace}" != identity:
        raise lock_io.LockError(
            f"{identity}: harness identity does not match the verified selection"
        )
    requires = document.get("requires")
    if not isinstance(requires, dict):
        raise lock_io.LockError(f"{identity}: harness requirements are invalid")
    harness_permissions.validate_requires(requires, f"{identity} harness")
    return resolved, requires


def _state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "claude_allow": [], "codex_rules": None}
    value = lock_io.load_json(path)
    if (
        set(value) != {"schema_version", "claude_allow", "codex_rules"}
        or value["schema_version"] != 1
    ):
        raise lock_io.LockError(
            f"{path}: invalid repository permission ownership state"
        )
    lock_io.require_string_list(
        value["claude_allow"], f"{path}: owned Claude approvals"
    )
    if value["codex_rules"] is not None and not isinstance(value["codex_rules"], str):
        raise lock_io.LockError(
            f"{path}: invalid repository permission ownership state"
        )
    return value


def _claude_plan(
    path: Path, desired: list[str], previous: Sequence[str]
) -> tuple[str, list[str]]:
    settings = lock_io.load_json(path) if path.exists() else {}
    permissions = settings.get("permissions", {})
    if not isinstance(permissions, dict):
        raise lock_io.LockError(f"{path}: permissions must be an object")
    existing = permissions.get("allow", [])
    lock_io.require_string_list(existing, f"{path}: Claude approvals")
    missing = sorted(set(previous) - set(existing))
    if missing:
        raise lock_io.LockError(
            f"{path}: owned Claude approval was changed: {missing[0]}"
        )
    survivors = [entry for entry in existing if entry not in previous]
    owned = [entry for entry in desired if entry not in survivors]
    permissions["allow"] = survivors + owned
    settings["permissions"] = permissions
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n", owned


def _rules_text(rules: list[JsonObject]) -> str | None:
    if not rules:
        return None
    return "\n".join([RULES_HEADER, "", *harness_permissions.codex_rule_lines(rules)])


def _rules_plan(path: Path, desired: str | None, previous: str | None) -> str | None:
    current = repo_config.text_at(path)
    if previous is not None and current != previous:
        raise lock_io.LockError(f"{path}: owned Codex rules were changed")
    if previous is None and current is not None and desired is not None:
        raise lock_io.LockError(f"{path}: refusing to replace unowned Codex rules")
    return desired


def _requirements(
    selected: Sequence[str], roots: Mapping[str, Path], project: Path
) -> tuple[list[str], list[JsonObject], list[Path]]:
    if len(selected) != len(set(selected)) or tuple(sorted(selected)) != tuple(
        selected
    ):
        raise lock_io.LockError("Selected plugin identities must be sorted and unique")
    if set(roots) != set(selected):
        raise lock_io.LockError(
            "Verified plugin roots do not match the selected plugin identities"
        )
    requirements: list[JsonObject] = []
    manifests = []
    for identity in selected:
        root, requires = _verified_root(identity, Path(roots[identity]))
        claude_allow, codex_rules = harness_permissions.render_permissions(
            requires["permissions"], root, project
        )
        requirements.append(
            {
                "permissions": {
                    "claude_allow": claude_allow,
                    "codex_prefix_rules": codex_rules,
                }
            }
        )
        manifests.append(root / "harness.json")
    claude_allow, codex_rules = harness_permissions.fold_permissions(requirements)
    return claude_allow, codex_rules, manifests


def apply(
    project: Path, selected: Sequence[str], roots: Mapping[str, Path]
) -> list[str]:
    project = project.expanduser().absolute()
    _require_direct_path(project)
    if tuple(selected) != plugin_selection.IDENTITY_RULES.selected(project):
        raise lock_io.LockError(
            "Permission selection does not match the target repository"
        )
    claude_allow, codex_rules, manifests = _requirements(selected, roots, project)
    state_path = project / STATE
    rules_path = project / CODEX_RULES
    claude_path = project / CLAUDE_SETTINGS
    needed = bool(claude_allow or codex_rules or state_path.exists())
    if not needed:
        return []
    for path in (claude_path, rules_path, state_path):
        _require_direct_path(path)
    exclude = repo_config.preflight_paths(
        project,
        [claude_path, rules_path, state_path],
        read_paths=manifests,
    )
    if exclude is not None:
        _require_direct_path(exclude)
    state = _state(state_path)
    claude_text, owned_allow = _claude_plan(
        claude_path, claude_allow, state["claude_allow"]
    )
    rules_text = _rules_text(codex_rules)
    rules_text = _rules_plan(rules_path, rules_text, state["codex_rules"])
    next_state = {
        "schema_version": 1,
        "claude_allow": owned_allow,
        "codex_rules": rules_text,
    }
    targets: dict[Path, str | None] = {
        claude_path: claude_text,
        state_path: json.dumps(next_state, indent=2, ensure_ascii=False) + "\n",
    }
    if rules_text is not None or state["codex_rules"] is not None:
        targets[rules_path] = rules_text
    if not owned_allow and rules_text is None:
        targets[state_path] = None
    if exclude is not None:
        targets[exclude] = repo_config.exclusion_text(
            exclude,
            project,
            [claude_path, rules_path, state_path],
        )
    changed = {
        path: text
        for path, text in targets.items()
        if repo_config.text_at(path) != text
    }
    repo_config.write_batch(changed)
    return [
        str(path.relative_to(project)) if path.is_relative_to(project) else str(path)
        for path in changed
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--roots-stdin", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin) if arguments.roots_stdin else {}
        selected = payload.get("selected") if isinstance(payload, dict) else None
        roots = payload.get("roots") if isinstance(payload, dict) else None
        if not isinstance(selected, list) or any(
            not isinstance(identity, str) for identity in selected
        ):
            raise lock_io.LockError("Verified plugin selection must be a string list")
        if not isinstance(roots, dict) or any(
            not isinstance(identity, str) or not isinstance(root, str)
            for identity, root in roots.items()
        ):
            raise lock_io.LockError(
                "Verified plugin roots must map identities to paths"
            )
        apply(
            arguments.root,
            selected,
            {identity: Path(root) for identity, root in roots.items()},
        )
    except (lock_io.LockError, OSError, UnicodeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
