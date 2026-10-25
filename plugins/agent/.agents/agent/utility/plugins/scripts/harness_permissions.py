#!/usr/bin/env python3
"""Compose and render shipped harness permission declarations (`requires.permissions`)."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

import lock_io


PLUGIN_ROOT_TOKEN = "${PLUGIN_ROOT}"
PROJECT_ROOT_TOKEN = "${PROJECT_ROOT}"
SCRIPT_SUFFIXES = (".py", ".ps1")


def _require_tokens(value: Any, label: str) -> None:
    if isinstance(value, str):
        if not value:
            raise lock_io.LockError(f"{label} must be a non-empty string")
        return
    if isinstance(value, list):
        if not value or any(not isinstance(item, str) or not item for item in value):
            raise lock_io.LockError(f"{label} must be a list of non-empty strings")
        return
    raise lock_io.LockError(f"{label} must be a string or a list of strings")


def _require_rule_field(value: Any, label: str) -> None:
    if not isinstance(value, list) or not value:
        raise lock_io.LockError(f"{label} must be a non-empty list")
    for index, item in enumerate(value):
        _require_tokens(item, f"{label}[{index}]")


def _has_unbounded_script_arguments(command: str) -> bool:
    if not command.startswith("Bash(") or not command.endswith(")"):
        return False
    if PROJECT_ROOT_TOKEN in command:
        return False
    body = command[len("Bash("):-1]
    # Exact native command rules keep their fixed arguments and need no wildcard.
    if not any(character in body for character in "*?[]"):
        return False
    if body.endswith(":*"):
        prefix = body[:-2]
    elif body.endswith(" *"):
        prefix = body[:-2]
    else:
        prefix = None
    if prefix and not any(character in prefix for character in "*?[]"):
        return False
    tokens = body.split()
    return any(token.lower().endswith(SCRIPT_SUFFIXES) for token in tokens[:-1])


def validate_requires(requires: dict, label: str) -> None:
    allowed = {"marketplaces", "plugins", "hooks", "permissions"}
    if not isinstance(requires, dict) or set(requires) not in (allowed, allowed | {"setup"}):
        raise lock_io.LockError(f"{label}: invalid harness requirements")
    if "setup" in requires:
        setup = requires["setup"]
        script = setup.get("script") if isinstance(setup, dict) and set(setup) == {"script"} else None
        path = Path(*PurePosixPath(script).parts) if isinstance(script, str) else None
        if not isinstance(script, str) or not script or path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            raise lock_io.LockError(f"{label}: invalid harness setup")
    if not isinstance(requires["marketplaces"], list) or not isinstance(requires["hooks"], list):
        raise lock_io.LockError(f"{label}: invalid marketplace or hook requirements")
    lock_io.require_string_list(requires["plugins"], f"{label}.plugins")
    permissions = requires["permissions"]
    if not isinstance(permissions, dict) or set(permissions) not in (
        {"claude_allow", "codex_prefix_rules"},
        {"claude_allow", "codex_prefix_rules", "auto_merge"},
    ):
        raise lock_io.LockError(f"{label}: invalid harness permissions")
    _validate_permissions(permissions, label)
    if "auto_merge" in permissions:
        conditional = permissions["auto_merge"]
        if not isinstance(conditional, dict) or set(conditional) != {"claude_allow", "codex_prefix_rules"}:
            raise lock_io.LockError(f"{label}: invalid auto-merge permissions")
        _validate_permissions(conditional, f"{label}.auto_merge")


def _validate_permissions(permissions: dict, label: str) -> None:
    lock_io.require_string_list(permissions["claude_allow"], f"{label}.claude_allow")
    for command in permissions["claude_allow"]:
        if _has_unbounded_script_arguments(command):
            raise lock_io.LockError(f"{label}.claude_allow script arguments require :*: {command}")
    rules = permissions["codex_prefix_rules"]
    if not isinstance(rules, list):
        raise lock_io.LockError(f"{label}: invalid Codex prefix rules")
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"pattern", "justification", "match", "not_match"}:
            raise lock_io.LockError(f"{label}: invalid Codex prefix rule")
        for field in ("pattern", "match", "not_match"):
            _require_rule_field(rule[field], f"{label}.{field}")
        lock_io.require_string(rule["justification"], f"{label}.justification")


def contains_template(value: Any) -> bool:
    if isinstance(value, str):
        return PLUGIN_ROOT_TOKEN in value
    if isinstance(value, list):
        return any(contains_template(item) for item in value)
    if isinstance(value, dict):
        return any(contains_template(item) for item in value.values())
    return False


def path_spellings(root: Path | str) -> tuple[str, str]:
    text = str(root)
    return text.replace("\\", "/"), text.replace("/", "\\")


def _forward_spelling(template: str, root_forward: str, project_forward: str = "") -> str:
    return template.replace(PLUGIN_ROOT_TOKEN, root_forward).replace(PROJECT_ROOT_TOKEN, project_forward).replace("\\", "/")


def _backslash_spelling(template: str, root_backslash: str, project_backslash: str = "") -> str:
    return template.replace(PLUGIN_ROOT_TOKEN, root_backslash).replace(PROJECT_ROOT_TOKEN, project_backslash).replace("/", "\\")


def render_claude_allow(templates: list[str], plugin_root: Path | str, project_root: Path | str | None = None) -> list[str]:
    root_forward, root_backslash = path_spellings(plugin_root)
    project_forward, project_backslash = path_spellings(project_root or "")
    rendered: list[str] = []
    for template in templates:
        if PLUGIN_ROOT_TOKEN in template:
            rendered.append(_forward_spelling(template, root_forward, project_forward))
            rendered.append(_backslash_spelling(template, root_backslash, project_backslash))
        else:
            rendered.append(template)
    return rendered


def _render_codex_token(token: Any, plugin_root: Path | str, alternate: bool, project_root: Path | str | None = None) -> Any:
    if isinstance(token, list):
        rendered: list[Any] = []
        for item in token:
            result = _render_codex_token(item, plugin_root, alternate, project_root)
            if isinstance(result, list):
                rendered.extend(result)
            else:
                rendered.append(result)
        return rendered
    if isinstance(token, str) and (PLUGIN_ROOT_TOKEN in token or PROJECT_ROOT_TOKEN in token):
        root_forward, root_backslash = path_spellings(plugin_root)
        project_forward, project_backslash = path_spellings(project_root or "")
        if alternate:
            return [_backslash_spelling(token, root_backslash, project_backslash), _forward_spelling(token, root_forward, project_forward)]
        return _backslash_spelling(token, root_backslash, project_backslash)
    return token


def render_codex_rule(rule: dict, plugin_root: Path | str, project_root: Path | str | None = None) -> dict:
    return {
        "pattern": [_render_codex_token(token, plugin_root, True, project_root) for token in rule["pattern"]],
        "justification": rule["justification"],
        "match": [_render_codex_token(example, plugin_root, False, project_root) for example in rule["match"]],
        "not_match": [_render_codex_token(example, plugin_root, False, project_root) for example in rule["not_match"]],
    }


def render_permissions(permissions: dict, plugin_root: Path | str, project_root: Path | str | None = None, auto_merge: bool = False) -> tuple[list[str], list[dict]]:
    conditional = permissions.get("auto_merge", {}) if auto_merge else {}
    claude_allow = render_claude_allow(permissions["claude_allow"] + conditional.get("claude_allow", []), plugin_root, project_root)
    codex_rules = [render_codex_rule(rule, plugin_root, project_root) for rule in permissions["codex_prefix_rules"] + conditional.get("codex_prefix_rules", [])]
    return claude_allow, codex_rules


def fold_permissions(requirements: list[dict]) -> tuple[list[str], list[dict]]:
    claude_allow: set[str] = set()
    codex_rules: dict[str, dict] = {}
    for requires in requirements:
        claude_allow.update(requires["permissions"]["claude_allow"])
        for rule in requires["permissions"]["codex_prefix_rules"]:
            codex_rules[json.dumps(rule, sort_keys=True)] = rule
    return sorted(claude_allow), [codex_rules[key] for key in sorted(codex_rules)]


def codex_rule_lines(rules: list[dict]) -> list[str]:
    lines: list[str] = []
    for rule in rules:
        lines += [
            "prefix_rule(",
            f"    pattern = {json.dumps(rule['pattern'], ensure_ascii=False)},",
            '    decision = "allow",',
            f"    justification = {json.dumps(rule['justification'], ensure_ascii=False)},",
            f"    match = {json.dumps(rule['match'], ensure_ascii=False)},",
            f"    not_match = {json.dumps(rule['not_match'], ensure_ascii=False)},",
            ")",
            "",
        ]
    return lines
