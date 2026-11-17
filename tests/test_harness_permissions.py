from typing import Any

import harness_permissions
import pytest
from lock_io import LockError

ALLOW_TJ_ITEM = {
    "pattern": ["tj", "item"],
    "justification": "Work items.",
    "match": [["tj", "item", "resolve", "461"]],
    "not_match": [["tj", "run"]],
}
FORBID_GH_LOGIN = {
    "pattern": ["gh", "auth", "login"],
    "justification": "Account-wide scopes.",
    "match": [["gh", "auth", "login"]],
    "not_match": [["gh", "auth", "status"]],
}


def requires(**permissions: object) -> dict[str, Any]:
    return {
        "marketplaces": [],
        "plugins": [],
        "hooks": [],
        "permissions": {"claude_allow": [], "codex_prefix_rules": [], **permissions},
    }


def test_validate_requires_accepts_deny_and_forbidden_rules() -> None:
    harness_permissions.validate_requires(
        requires(
            claude_allow=["Bash(tj item:*)"],
            claude_deny=["Bash(gh auth login *)"],
            codex_prefix_rules=[ALLOW_TJ_ITEM],
            codex_forbidden_prefix_rules=[FORBID_GH_LOGIN],
        ),
        "plugin",
    )


@pytest.mark.parametrize(
    ("permissions", "field"),
    [
        ({"claude_allow": ["Bash(python -B ${PLUGIN_ROOT}/x.py)"]}, "claude_allow"),
        ({"claude_deny": ["Read(C:/Users/name/.ssh/**)"]}, "claude_deny"),
        ({"claude_deny": [r"Bash(\\server\share\x.ps1)"]}, "claude_deny"),
        (
            {"codex_prefix_rules": [{**ALLOW_TJ_ITEM, "pattern": ["/home/name/x"]}]},
            "codex_prefix_rules",
        ),
        (
            {
                "codex_forbidden_prefix_rules": [
                    {**FORBID_GH_LOGIN, "match": [["gh", "${PROJECT_ROOT}"]]}
                ]
            },
            "codex_forbidden_prefix_rules",
        ),
    ],
)
def test_validate_requires_rejects_machine_specific_rules(
    permissions: dict[str, Any], field: str
) -> None:
    with pytest.raises(LockError, match=rf"{field}.*machine-specific path"):
        harness_permissions.validate_requires(requires(**permissions), "plugin")


@pytest.mark.parametrize(
    ("permissions", "message"),
    [
        ({"claude_deny": ["Bash(x)", "Bash(x)"]}, "claude_deny contains duplicates"),
        ({"claude_deny": [""]}, "claude_deny must be a list of non-empty strings"),
        (
            {"codex_forbidden_prefix_rules": [FORBID_GH_LOGIN, FORBID_GH_LOGIN]},
            "codex_forbidden_prefix_rules contains duplicates",
        ),
        (
            {"codex_forbidden_prefix_rules": [{**FORBID_GH_LOGIN, "match": []}]},
            "match must be a non-empty list",
        ),
        (
            {
                "codex_forbidden_prefix_rules": [
                    {**FORBID_GH_LOGIN, "justification": ""}
                ]
            },
            "justification must be a non-empty string",
        ),
        (
            {"codex_forbidden_prefix_rules": [{"pattern": ["gh"]}]},
            "invalid Codex prefix rule",
        ),
        (
            {"claude_allow": ["Bash(x)"], "claude_deny": ["Bash(x)"]},
            "both allows and denies",
        ),
        (
            {
                "codex_prefix_rules": [ALLOW_TJ_ITEM],
                "codex_forbidden_prefix_rules": [
                    {**FORBID_GH_LOGIN, "pattern": ALLOW_TJ_ITEM["pattern"]}
                ],
            },
            "both allows and forbids",
        ),
        ({"unknown": []}, "invalid harness permissions"),
    ],
)
def test_validate_requires_rejects_invalid_deny_and_forbidden_rules(
    permissions: dict[str, Any], message: str
) -> None:
    with pytest.raises(LockError, match=message):
        harness_permissions.validate_requires(requires(**permissions), "plugin")


def test_compose_lets_a_plugin_deny_remove_another_plugins_allow() -> None:
    allowing = {
        "claude_allow": ["Bash(gh auth login *)", "Bash(tj item:*)"],
        "codex_prefix_rules": [
            {**ALLOW_TJ_ITEM, "pattern": FORBID_GH_LOGIN["pattern"]}
        ],
    }
    denying = {
        "claude_allow": [],
        "claude_deny": ["Bash(gh auth login *)"],
        "codex_prefix_rules": [ALLOW_TJ_ITEM],
        "codex_forbidden_prefix_rules": [FORBID_GH_LOGIN],
    }

    composed = harness_permissions.compose(
        [allowing, denying], harness_permissions.EMPTY_PERMISSIONS
    )

    assert composed == harness_permissions.Permissions(
        claude_allow=("Bash(tj item:*)",),
        claude_deny=("Bash(gh auth login *)",),
        codex_allow=(ALLOW_TJ_ITEM,),
        codex_forbidden=(FORBID_GH_LOGIN,),
    )


def test_compose_lets_the_repository_overlay_extend_and_override_plugins() -> None:
    plugin = {
        "claude_allow": ["Bash(git push:*)", "Bash(tj item:*)"],
        "claude_deny": ["Bash(gh auth login *)"],
        "codex_prefix_rules": [ALLOW_TJ_ITEM],
        "codex_forbidden_prefix_rules": [FORBID_GH_LOGIN],
    }
    overlay_rule = {**ALLOW_TJ_ITEM, "pattern": FORBID_GH_LOGIN["pattern"]}
    overlay = {
        "claude_allow": ["Bash(gh auth login *)", "Edit(/docs/**)"],
        "claude_deny": ["Bash(git push:*)"],
        "codex_prefix_rules": [overlay_rule],
        "codex_forbidden_prefix_rules": [],
    }

    composed = harness_permissions.compose([plugin], overlay)

    assert composed == harness_permissions.Permissions(
        claude_allow=("Bash(gh auth login *)", "Bash(tj item:*)", "Edit(/docs/**)"),
        claude_deny=("Bash(git push:*)",),
        codex_allow=tuple(
            sorted([ALLOW_TJ_ITEM, overlay_rule], key=harness_permissions.rule_key)
        ),
        codex_forbidden=(),
    )


def test_codex_rule_lines_render_each_decision() -> None:
    text = "\n".join(
        harness_permissions.codex_rule_lines([FORBID_GH_LOGIN], "forbidden")
    )

    assert 'pattern = ["gh", "auth", "login"],' in text
    assert 'decision = "forbidden",' in text
