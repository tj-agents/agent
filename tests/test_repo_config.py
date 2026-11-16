import importlib
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "plugins/agent/.agents/agent/utility/plugins/scripts"
)
sys.path.insert(0, str(SCRIPTS))
REPO_CONFIG: Any = importlib.import_module("repo_config")


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # A zero count makes git ignore any injected GIT_CONFIG_KEY_n/VALUE_n pairs.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git(tmp_path, "init", "--quiet")
    return tmp_path


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, timeout=15, check=True
    )


def test_remote_owners_reads_every_fetch_url_with_its_rewrite(
    repository: Path,
) -> None:
    git(repository, "remote", "add", "origin", "https://github.com/first/x.git")
    git(
        repository,
        "remote",
        "set-url",
        "--add",
        "origin",
        "https://github.com/second/y.git",
    )
    git(repository, "remote", "set-url", "--push", "origin", "https://github.com/p/q")
    git(repository, "config", "url.https://github.com/rewritten/.insteadOf", "gh:")
    git(repository, "remote", "add", "short", "gh:z.git")
    assert REPO_CONFIG.remote_owners(repository) == {"first", "second", "rewritten"}


def test_remote_owners_is_empty_without_remotes(repository: Path) -> None:
    assert REPO_CONFIG.remote_owners(repository) == set()


def test_preflight_rejects_a_root_below_the_checkout(repository: Path) -> None:
    nested = repository / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    with pytest.raises(REPO_CONFIG.lock_io.LockError, match="exact Git checkout"):
        REPO_CONFIG.preflight_paths(nested, [Path("settings.json")], scope="project")


def test_preflight_returns_the_local_exclusion_file(repository: Path) -> None:
    exclude = REPO_CONFIG.preflight_paths(
        repository, [Path(".claude/settings.local.json")], scope="local"
    )
    assert exclude.resolve() == (repository / ".git/info/exclude").resolve()
    assert (
        REPO_CONFIG.preflight_paths(
            repository, [Path(".claude/settings.json")], scope="project"
        )
        is None
    )


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
DECLARED: dict[str, dict[str, Any]] = {
    "alpha@tj-agents": {
        "claude_allow": ["Bash(tj item:*)"],
        "claude_deny": ["Bash(gh auth login *)"],
        "codex_prefix_rules": [ALLOW_TJ_ITEM],
        "codex_forbidden_prefix_rules": [FORBID_GH_LOGIN],
    },
    "beta@tj-agents": {"claude_allow": ["Bash(uv sync)"], "codex_prefix_rules": []},
}


def select(root: Path, *plugins: str) -> Path:
    selection = root / ".agents/plugins.json"
    selection.parent.mkdir(exist_ok=True)
    selection.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "marketplaces": {"tj-agents": {"repository": "tj-agents/marketplace"}},
                "plugins": list(plugins),
            }
        ),
        encoding="utf-8",
    )
    return selection


def overlay(root: Path, permissions: dict[str, Any]) -> None:
    (root / ".agents/repository-harness.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "requires": {
                    "marketplaces": [],
                    "plugins": [],
                    "hooks": [],
                    "permissions": {
                        "claude_allow": [],
                        "codex_prefix_rules": [],
                        **permissions,
                    },
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Serve DECLARED as the selected plugins' published permissions; record lookups."""
    lookups: list[list[str]] = []

    def declarations(
        marketplaces: Mapping[str, Mapping[str, str]], identities: Iterable[str]
    ) -> dict[str, dict[str, Any]]:
        lookups.append(sorted(identities))
        return {identity: DECLARED[identity] for identity in identities}

    monkeypatch.setattr(REPO_CONFIG.published_harness, "declarations", declarations)
    return lookups


def permissions(root: Path) -> dict[str, list[str]]:
    settings = json.loads((root / ".claude/settings.json").read_text(encoding="utf-8"))
    return dict(settings["permissions"])


def rules(root: Path) -> str | None:
    path = root / ".codex/rules/agent-harness.rules"
    return path.read_text(encoding="utf-8") if path.is_file() else None


def test_run_commits_selected_plugins_allow_and_deny_rules(
    repository: Path, published: list[list[str]]
) -> None:
    selection = select(repository, "alpha@tj-agents", "beta@tj-agents")

    REPO_CONFIG.run(selection, "write")

    assert permissions(repository) == {
        "allow": ["Bash(tj item:*)", "Bash(uv sync)"],
        "deny": ["Bash(gh auth login *)"],
    }
    text = rules(repository)
    assert text is not None
    assert text.count('decision = "allow"') == 1
    assert text.count('decision = "forbidden"') == 1
    assert 'pattern = ["gh", "auth", "login"],\n    decision = "forbidden"' in text
    assert published == [["alpha@tj-agents", "beta@tj-agents"]]
    assert REPO_CONFIG.run(selection, "check") == []


def test_run_lets_the_repository_overlay_extend_and_override_plugins(
    repository: Path, published: list[list[str]]
) -> None:
    selection = select(repository, "alpha@tj-agents")
    overlay(
        repository,
        {
            "claude_allow": ["Bash(gh auth login *)", "Edit(/docs/**)"],
            "claude_deny": ["Bash(tj item:*)"],
            "codex_prefix_rules": [
                {**ALLOW_TJ_ITEM, "pattern": ["gh", "auth", "login"]}
            ],
        },
    )

    REPO_CONFIG.run(selection, "write")

    assert permissions(repository) == {
        "allow": ["Bash(gh auth login *)", "Edit(/docs/**)"],
        "deny": ["Bash(tj item:*)"],
    }
    text = rules(repository)
    assert text is not None
    assert 'decision = "forbidden"' not in text
    assert text.count('decision = "allow"') == 2


def test_deselecting_a_plugin_reports_drift_then_removes_its_rules(
    repository: Path, published: list[list[str]]
) -> None:
    selection = select(repository, "alpha@tj-agents", "beta@tj-agents")
    REPO_CONFIG.run(selection, "write")
    git(repository, "add", "--all")
    git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=f@example.invalid",
        "commit",
        "--quiet",
        "--message",
        "Adopt plugins",
    )
    select(repository, "beta@tj-agents")

    assert REPO_CONFIG.run(selection, "check") == [
        ".claude/settings.json",
        ".codex/config.toml",
        ".codex/rules/agent-harness.rules",
    ]
    REPO_CONFIG.run(selection, "write")

    assert permissions(repository) == {"allow": ["Bash(uv sync)"]}
    assert rules(repository) is None
    assert REPO_CONFIG.run(selection, "check") == []


def test_run_rejects_deny_entries_nobody_declares(
    repository: Path, published: list[list[str]]
) -> None:
    selection = select(repository, "beta@tj-agents")
    settings = repository / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {"permissions": {"deny": ["Edit(/.agents/plugins/marketplace.json)"]}}
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        REPO_CONFIG.lock_io.LockError, match=r"unmanaged permissions\.deny entries"
    ):
        REPO_CONFIG.run(selection, "write")


def test_run_rejects_an_overlay_rule_naming_a_machine_path(
    repository: Path, published: list[list[str]]
) -> None:
    selection = select(repository, "beta@tj-agents")
    overlay(repository, {"claude_deny": ["Read(C:/Users/name/.ssh/**)"]})

    with pytest.raises(REPO_CONFIG.lock_io.LockError, match="machine-specific path"):
        REPO_CONFIG.run(selection, "write")
    assert not (repository / ".claude").exists()


def test_preserved_permissions_need_no_published_lookup(
    repository: Path, published: list[list[str]]
) -> None:
    selection = select(repository, "alpha@tj-agents")
    REPO_CONFIG.run(selection, "write")
    committed = permissions(repository)
    committed_rules = rules(repository)
    select(repository, "alpha@tj-agents", "beta@tj-agents")

    assert REPO_CONFIG.run(selection, "write", preserve_permissions=True) == [
        ".claude/settings.json",
        ".codex/config.toml",
    ]

    assert published == [["alpha@tj-agents"]]
    assert permissions(repository) == committed
    assert rules(repository) == committed_rules
    settings = json.loads(
        (repository / ".claude/settings.json").read_text(encoding="utf-8")
    )
    assert "beta@tj-agents" in settings["enabledPlugins"]


def test_run_writes_nothing_to_user_settings(
    repository: Path,
    published: list[list[str]],
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path_factory.mktemp("home")
    claude = home / ".claude/settings.json"
    claude.parent.mkdir()
    claude.write_text('{"permissions": {"allow": ["Bash(mine)"]}}\n', encoding="utf-8")
    for variable in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(variable, str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(home / ".agents-state"))
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}

    REPO_CONFIG.run(select(repository, "alpha@tj-agents"), "write")

    assert {
        path: path.read_bytes() for path in home.rglob("*") if path.is_file()
    } == before
    assert permissions(repository)["deny"] == ["Bash(gh auth login *)"]
