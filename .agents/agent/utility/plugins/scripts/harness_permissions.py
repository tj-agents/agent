#!/usr/bin/env python3
"""Validate and compose harness permission declarations (`requires.permissions`).

Selected plugins and a repository's own overlay declare Claude `allow`/`deny` entries and Codex prefix rules
(`allow` or `forbidden`). They are committed literally into that repository's settings, so a declaration
naming a placeholder such as `${PLUGIN_ROOT}` or a machine-specific absolute path is rejected.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import lock_io
import machine_paths

PLACEHOLDER = re.compile(r"\$\{[^}]*\}")
SCRIPT_SUFFIXES = (".py", ".ps1")
CLAUDE_LISTS = ("claude_allow", "claude_deny")
CODEX_LISTS = ("codex_prefix_rules", "codex_forbidden_prefix_rules")
REQUIRED_PERMISSIONS = frozenset({"claude_allow", "codex_prefix_rules"})
OPTIONAL_PERMISSIONS = frozenset(
    {"claude_deny", "codex_forbidden_prefix_rules", "auto_merge"}
)
RULE_FIELDS = frozenset({"pattern", "justification", "match", "not_match"})
EMPTY_PERMISSIONS: Mapping[str, list[Any]] = {
    "claude_allow": [],
    "codex_prefix_rules": [],
}


@dataclass(frozen=True, slots=True)
class Permissions:
    """The entries one repository commits to its Claude settings and Codex rules."""

    claude_allow: tuple[str, ...] = ()
    claude_deny: tuple[str, ...] = ()
    codex_allow: tuple[dict[str, Any], ...] = ()
    codex_forbidden: tuple[dict[str, Any], ...] = ()


def _require_tokens(value: object, label: str) -> None:
    if isinstance(value, str):
        if not value:
            raise lock_io.LockError(f"{label} must be a non-empty string")
        return
    if isinstance(value, list):
        if not value or any(not isinstance(item, str) or not item for item in value):
            raise lock_io.LockError(f"{label} must be a list of non-empty strings")
        return
    raise lock_io.LockError(f"{label} must be a string or a list of strings")


def _require_rule_field(value: object, label: str) -> None:
    if not isinstance(value, list) or not value:
        raise lock_io.LockError(f"{label} must be a non-empty list")
    for index, item in enumerate(value):
        _require_tokens(item, f"{label}[{index}]")


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _require_portable(value: object, label: str) -> None:
    for text in _strings(value):
        if PLACEHOLDER.search(text) or machine_paths.MACHINE_PATH.search(text):
            raise lock_io.LockError(
                f"{label} names a machine-specific path, which a repository cannot "
                f"commit: {text}. Declare a command found on PATH or a "
                "repository-relative path."
            )


def _has_unbounded_script_arguments(command: str) -> bool:
    if not command.startswith("Bash(") or not command.endswith(")"):
        return False
    body = command[len("Bash(") : -1]
    # Exact native command rules keep their fixed arguments and need no wildcard.
    if not any(character in body for character in "*?[]"):
        return False
    prefix = body[:-2] if body.endswith((":*", " *")) else None
    if prefix and not any(character in prefix for character in "*?[]"):
        return False
    tokens = body.split()
    return any(token.lower().endswith(SCRIPT_SUFFIXES) for token in tokens[:-1])


def rule_key(rule: Mapping[str, object]) -> str:
    return json.dumps(rule, sort_keys=True, ensure_ascii=False)


def pattern_key(rule: Mapping[str, object]) -> str:
    return json.dumps(rule["pattern"], ensure_ascii=False)


def validate_requires(requires: object, label: str) -> None:
    allowed = {"marketplaces", "plugins", "hooks", "permissions", "setup"}
    if (
        not isinstance(requires, dict)
        or not {"marketplaces", "plugins"} <= set(requires)
        or set(requires) - allowed
    ):
        raise lock_io.LockError(f"{label}: invalid harness requirements")
    if "setup" in requires:
        setup = requires["setup"]
        valid_setup = (
            isinstance(setup, dict)
            and set(setup) in ({"script"}, {"script", "converge"})
            and ("converge" not in setup or isinstance(setup["converge"], bool))
        )
        script = setup.get("script") if valid_setup else None
        path = Path(*PurePosixPath(script).parts) if isinstance(script, str) else None
        if (
            path is None
            or not script
            or path.is_absolute()
            or ".." in path.parts
            or path.suffix != ".py"
        ):
            raise lock_io.LockError(f"{label}: invalid harness setup")
    if not isinstance(requires["marketplaces"], list) or not isinstance(
        requires.get("hooks", []), list
    ):
        raise lock_io.LockError(f"{label}: invalid marketplace or hook requirements")
    lock_io.require_string_list(requires["plugins"], f"{label}.plugins")
    permissions = requires.get("permissions", EMPTY_PERMISSIONS)
    if (
        not isinstance(permissions, dict)
        or not REQUIRED_PERMISSIONS <= set(permissions)
        or set(permissions) - REQUIRED_PERMISSIONS - OPTIONAL_PERMISSIONS
    ):
        raise lock_io.LockError(f"{label}: invalid harness permissions")
    _validate_permissions(permissions, label)
    if "auto_merge" in permissions:
        conditional = permissions["auto_merge"]
        if (
            not isinstance(conditional, dict)
            or set(conditional) != REQUIRED_PERMISSIONS
        ):
            raise lock_io.LockError(f"{label}: invalid auto-merge permissions")
        _validate_permissions(conditional, f"{label}.auto_merge")


def _validate_rules(rules: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(rules, list):
        raise lock_io.LockError(f"{label} must be a list of Codex prefix rules")
    seen: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != RULE_FIELDS:
            raise lock_io.LockError(f"{label}: invalid Codex prefix rule")
        for field in ("pattern", "match", "not_match"):
            _require_rule_field(rule[field], f"{label}.{field}")
        lock_io.require_string(rule["justification"], f"{label}.justification")
        _require_portable(rule, label)
        key = rule_key(rule)
        if key in seen:
            raise lock_io.LockError(f"{label} contains duplicates")
        seen.add(key)
    return rules


def _validate_permissions(permissions: Mapping[str, object], label: str) -> None:
    claude: dict[str, list[str]] = {}
    for key in CLAUDE_LISTS:
        entries = lock_io.require_string_list(
            permissions.get(key, []), f"{label}.{key}"
        )
        for entry in entries:
            _require_portable(entry, f"{label}.{key}")
        claude[key] = entries
    for command in claude["claude_allow"]:
        if _has_unbounded_script_arguments(command):
            raise lock_io.LockError(
                f"{label}.claude_allow script arguments require :*: {command}"
            )
    contradictory = sorted(set(claude["claude_allow"]) & set(claude["claude_deny"]))
    if contradictory:
        raise lock_io.LockError(
            f"{label} both allows and denies: " + ", ".join(contradictory)
        )
    codex = {
        key: _validate_rules(permissions.get(key, []), f"{label}.{key}")
        for key in CODEX_LISTS
    }
    allowed = {pattern_key(rule) for rule in codex["codex_prefix_rules"]}
    forbidden = {pattern_key(rule) for rule in codex["codex_forbidden_prefix_rules"]}
    if allowed & forbidden:
        raise lock_io.LockError(
            f"{label} both allows and forbids Codex patterns: "
            + ", ".join(sorted(allowed & forbidden))
        )


def _rules(rules: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {rule_key(rule): dict(rule) for rule in rules}


def compose(
    declared: Iterable[Mapping[str, Any]], overlay: Mapping[str, Any]
) -> Permissions:
    """Merge validated plugin declarations, then apply the repository overlay.

    Plugins combine as a union in which a deny or forbidden rule removes the identical
    allow entry or Codex pattern. The overlay extends that result and wins: its entry
    replaces the same Claude entry or Codex pattern from any plugin, whichever way the
    plugin decided it.
    """
    allow: set[str] = set()
    deny: set[str] = set()
    codex_allow: dict[str, dict[str, Any]] = {}
    codex_forbidden: dict[str, dict[str, Any]] = {}
    for permissions in declared:
        allow.update(permissions["claude_allow"])
        deny.update(permissions.get("claude_deny", []))
        codex_allow.update(_rules(permissions["codex_prefix_rules"]))
        codex_forbidden.update(
            _rules(permissions.get("codex_forbidden_prefix_rules", []))
        )
    allow -= deny
    forbidden_patterns = {pattern_key(rule) for rule in codex_forbidden.values()}
    codex_allow = {
        key: rule
        for key, rule in codex_allow.items()
        if pattern_key(rule) not in forbidden_patterns
    }
    overlay_allow = set(overlay["claude_allow"])
    overlay_deny = set(overlay.get("claude_deny", []))
    overlay_codex_allow = _rules(overlay["codex_prefix_rules"])
    overlay_codex_forbidden = _rules(overlay.get("codex_forbidden_prefix_rules", []))
    overlay_patterns = {
        pattern_key(rule)
        for rule in (*overlay_codex_allow.values(), *overlay_codex_forbidden.values())
    }
    allow = (allow - overlay_deny) | overlay_allow
    deny = (deny - overlay_allow) | overlay_deny
    codex_allow = {
        key: rule
        for key, rule in codex_allow.items()
        if pattern_key(rule) not in overlay_patterns
    } | overlay_codex_allow
    codex_forbidden = {
        key: rule
        for key, rule in codex_forbidden.items()
        if pattern_key(rule) not in overlay_patterns
    } | overlay_codex_forbidden
    return Permissions(
        claude_allow=tuple(sorted(allow)),
        claude_deny=tuple(sorted(deny)),
        codex_allow=tuple(codex_allow[key] for key in sorted(codex_allow)),
        codex_forbidden=tuple(codex_forbidden[key] for key in sorted(codex_forbidden)),
    )


def codex_rule_lines(rules: Iterable[Mapping[str, Any]], decision: str) -> list[str]:
    lines: list[str] = []
    for rule in rules:
        lines += [
            "prefix_rule(",
            f"    pattern = {json.dumps(rule['pattern'], ensure_ascii=False)},",
            f"    decision = {json.dumps(decision)},",
            "    justification = "
            f"{json.dumps(rule['justification'], ensure_ascii=False)},",
            f"    match = {json.dumps(rule['match'], ensure_ascii=False)},",
            f"    not_match = {json.dumps(rule['not_match'], ensure_ascii=False)},",
            ")",
            "",
        ]
    return lines
