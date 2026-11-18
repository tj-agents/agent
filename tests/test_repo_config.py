import importlib
import json
import os
import subprocess
import sys
import tomllib
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
ADOPT: Any = importlib.import_module("adopt_plugins")
TJ_PREPARE: Any = importlib.import_module("tj_prepare")


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # A zero count makes git ignore any injected GIT_CONFIG_KEY_n/VALUE_n pairs.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git(tmp_path, "init", "--quiet")
    return tmp_path


def git(root: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, (
        f"git {arguments!r} exited {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )


@pytest.mark.parametrize("scope", ["project", "local"])
def test_explicit_scope_does_not_depend_on_repository_owner(
    repository: Path, scope: str
) -> None:
    git(
        repository, "remote", "add", "origin", "https://github.com/example-user/app.git"
    )
    assert REPO_CONFIG.resolve_scope(repository, scope) == scope


@pytest.mark.parametrize("owner", [None, "example-user", "tj-agents", "Concertable"])
def test_untracked_selection_defaults_local_regardless_of_owner(
    repository: Path, owner: str | None
) -> None:
    if owner is not None:
        git(
            repository, "remote", "add", "origin", f"https://github.com/{owner}/app.git"
        )
    select(repository, "beta@tj-agents")
    assert REPO_CONFIG.resolve_scope(repository, None) == "local"


def test_a_missing_selection_defaults_local(repository: Path) -> None:
    assert REPO_CONFIG.resolve_scope(repository, None) == "local"


def test_tracked_selection_preserves_project_scope(repository: Path) -> None:
    git(
        repository, "remote", "add", "origin", "https://github.com/example-user/app.git"
    )
    select(repository, "beta@tj-agents")
    git(repository, "add", ".agents/plugins.json")
    assert REPO_CONFIG.resolve_scope(repository, None) == "project"
    assert REPO_CONFIG.resolve_scope(repository, "local") == "local"


def test_selection_scope_does_not_guess_after_git_failure(repository: Path) -> None:
    def failed(
        arguments: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            arguments, 128, stdout="", stderr="index unavailable"
        )

    with pytest.raises(REPO_CONFIG.lock_io.LockError, match="index unavailable"):
        REPO_CONFIG.resolve_scope(repository, None, run=failed)


def test_unversioned_root_defaults_project(tmp_path: Path) -> None:
    assert REPO_CONFIG.resolve_scope(tmp_path, None) == "project"
    assert REPO_CONFIG.resolve_scope(tmp_path, "local") == "local"


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


def test_project_adoption_unwinds_local_ownership_and_preserves_authored_settings(
    repository: Path, published: list[list[str]]
) -> None:
    claude = repository / ".claude/settings.local.json"
    claude.parent.mkdir()
    claude.write_text(json.dumps({
        "custom": "keep", "enabledPlugins": {"other@else": True},
        "permissions": {"allow": ["Bash(mine)"]},
    }), encoding="utf-8")
    codex = repository / ".codex/config.toml"
    codex.parent.mkdir()
    codex.write_text(
        'model = "my-model"\n[plugins."other@else"]\nenabled = true\n', encoding="utf-8"
    )
    exclude = repository / ".git/info/exclude"
    exclude.write_text("# authored\nmy-file\n", encoding="utf-8")
    roster = ADOPT.load_roster()
    roster["plugins"]["beta"] = []
    ADOPT.run(repository, [], ["beta"], "write", scope="local", roster=roster)
    with pytest.raises(TJ_PREPARE.standards.StandardsError, match="Local-only"):
        TJ_PREPARE.check_instruction_scope(repository)

    ADOPT.run(repository, [], ["beta"], "write", scope="project", roster=roster)

    TJ_PREPARE.check_instruction_scope(repository)
    assert not (repository / ".agents/repo-capabilities.local.json").exists()
    local = json.loads(claude.read_text(encoding="utf-8"))
    assert local["custom"] == "keep"
    assert local["enabledPlugins"] == {"other@else": True}
    assert local["permissions"]["allow"] == ["Bash(mine)"]
    assert tomllib.loads(codex.read_text(encoding="utf-8"))["model"] == "my-model"
    assert exclude.read_text(encoding="utf-8") == "# authored\nmy-file\n"
    assert ADOPT.run(
        repository, [], ["beta"], "write", scope="project", roster=roster
    ) == []
    config = tomllib.loads(codex.read_text(encoding="utf-8"))
    assert config["plugins"]["other@else"] == {"enabled": True}


@pytest.mark.parametrize("changed", ["claude", "codex", "rules", "exclude"])
def test_project_adoption_refuses_changed_local_owned_content(
    repository: Path, published: list[list[str]], changed: str
) -> None:
    roster = ADOPT.load_roster()
    roster["plugins"]["alpha"] = []
    ADOPT.run(repository, [], ["alpha"], "write", scope="local", roster=roster)
    paths = {
        "claude": repository / ".claude/settings.local.json",
        "codex": repository / ".codex/config.toml",
        "rules": repository / ".codex/rules/agent-harness.rules",
        "exclude": repository / ".git/info/exclude",
    }
    if changed == "claude":
        settings = json.loads(paths[changed].read_text(encoding="utf-8"))
        settings["enabledPlugins"]["alpha@tj-agents"] = False
        paths[changed].write_text(json.dumps(settings), encoding="utf-8")
    elif changed == "exclude":
        text = paths[changed].read_text(encoding="utf-8")
        paths[changed].write_text(
            text.replace(
                REPO_CONFIG.EXCLUDE_END, "authored\n" + REPO_CONFIG.EXCLUDE_END
            ),
            encoding="utf-8",
        )
    else:
        paths[changed].write_text("changed\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in paths.values()}
    marker = repository / REPO_CONFIG.LOCAL_STATE
    marker_before = marker.read_bytes()

    with pytest.raises(REPO_CONFIG.lock_io.LockError, match=r"changed|owned"):
        ADOPT.run(repository, [], ["alpha"], "write", scope="project", roster=roster)

    assert {path: path.read_bytes() for path in paths.values()} == before
    assert marker.read_bytes() == marker_before
    assert not (repository / ".claude/settings.json").exists()


@pytest.mark.parametrize("existing_settings", [False, True])
def test_adoption_without_permission_choice_preserves_native_controls(
    repository: Path, published: list[list[str]], existing_settings: bool
) -> None:
    selection = select(repository, "beta@tj-agents")
    claude = repository / ".claude/settings.json"
    codex = repository / ".codex/config.toml"
    native_claude = {"defaultMode": "plan", "disableBypassPermissionsMode": "disable"}
    native_codex = {"approval_policy": "untrusted", "sandbox_mode": "read-only"}
    if existing_settings:
        claude.parent.mkdir()
        claude.write_text(json.dumps({"permissions": native_claude}), encoding="utf-8")
        codex.parent.mkdir()
        codex.write_text(
            'approval_policy = "untrusted"\nsandbox_mode = "read-only"\n',
            encoding="utf-8",
        )

    REPO_CONFIG.run(selection, "write", scope="project")

    actual_claude = json.loads(claude.read_text(encoding="utf-8"))["permissions"]
    actual_codex = tomllib.loads(codex.read_text(encoding="utf-8"))
    assert actual_claude == {
        "allow": ["Bash(uv sync)"],
        **(native_claude if existing_settings else {}),
    }
    for key, value in native_codex.items():
        if existing_settings:
            assert actual_codex[key] == value
        else:
            assert key not in actual_codex
    assert "default_permissions" not in actual_codex
    assert published == [["beta@tj-agents"]]


def test_run_commits_selected_plugins_allow_and_deny_rules(
    repository: Path, published: list[list[str]]
) -> None:
    selection = select(repository, "alpha@tj-agents", "beta@tj-agents")

    REPO_CONFIG.run(selection, "write", scope="project")

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
    assert REPO_CONFIG.run(selection, "check", scope="project") == []


@pytest.fixture
def inherited_checkout(tmp_path: Path, plugin_source: Path) -> tuple[Path, Path]:
    primary = plugin_source
    (primary / "README.md").write_text("primary\n", encoding="utf-8")
    git(primary, "add", "README.md")
    git(primary, "commit", "-qm", "initial")
    linked = tmp_path / "linked"
    git(primary, "worktree", "add", "--quiet", str(linked))
    select(primary, "alpha@tj-agents")
    overlay(primary, {"claude_allow": ["Bash(local overlay:*)"]})
    return primary, linked


@pytest.mark.parametrize("scope", [None, "local", "project"])
@pytest.mark.parametrize("explicit_lock", [False, True])
def test_main_reads_a_primary_selection_and_overlay_but_writes_to_the_linked_worktree(
    inherited_checkout: tuple[Path, Path],
    published: list[list[str]],
    scope: str | None,
    explicit_lock: bool,
) -> None:
    primary, linked = inherited_checkout
    inherited = REPO_CONFIG.plugin_selection.resolve(linked)
    assert inherited == primary / ".agents/plugins.json"
    before = inherited.read_bytes()
    arguments = ["--root", str(linked), "--mode", "write"]
    if scope is not None:
        arguments.extend(["--scope", scope])
    if explicit_lock:
        arguments.extend(["--lock", str(inherited)])
    assert REPO_CONFIG.main(arguments) == 0

    filename = "settings.json" if scope == "project" else "settings.local.json"
    linked_settings = linked / ".claude" / filename
    assert linked_settings.is_file()
    assert "Bash(local overlay:*)" in linked_settings.read_text(encoding="utf-8")
    claude = json.loads(linked_settings.read_text(encoding="utf-8"))
    codex = tomllib.loads((linked / ".codex/config.toml").read_text(encoding="utf-8"))
    assert "defaultMode" not in claude["permissions"]
    assert (
        not {"approval_policy", "default_permissions", "sandbox_mode", "projects"}
        & codex.keys()
    )
    assert (linked / ".agents/repo-capabilities.local.json").exists() == (
        scope != "project"
    )
    assert inherited.read_bytes() == before
    assert not (linked / ".agents/plugins.json").exists()
    assert not (primary / ".claude").exists()
    assert not (primary / ".codex").exists()
    assert published == [["alpha@tj-agents"]]


@pytest.mark.parametrize("mode", ["check", "write"])
@pytest.mark.parametrize("malformed", ["json", "directory"])
def test_main_refuses_a_malformed_local_selection_instead_of_inheriting(
    inherited_checkout: tuple[Path, Path],
    published: list[list[str]],
    capsys: pytest.CaptureFixture[str],
    mode: str,
    malformed: str,
) -> None:
    primary, linked = inherited_checkout
    local = linked / ".agents/plugins.json"
    local.parent.mkdir()
    if malformed == "json":
        local.write_text("{broken", encoding="utf-8")
    else:
        local.mkdir()
    primary_selection = (primary / ".agents/plugins.json").read_bytes()

    assert REPO_CONFIG.main(["--root", str(linked), "--mode", mode]) == 1

    assert "Cannot read JSON" in capsys.readouterr().err
    assert not published
    assert not (linked / ".claude").exists()
    assert not (linked / ".codex").exists()
    assert not (linked / ".agents/repo-capabilities.local.json").exists()
    assert (primary / ".agents/plugins.json").read_bytes() == primary_selection
    if malformed == "json":
        assert local.read_text(encoding="utf-8") == "{broken"
    else:
        assert local.is_dir()


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

    REPO_CONFIG.run(selection, "write", scope="project")

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
    REPO_CONFIG.run(selection, "write", scope="project")
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

    assert REPO_CONFIG.run(selection, "check", scope="project") == [
        ".claude/settings.json",
        ".codex/config.toml",
        ".codex/rules/agent-harness.rules",
    ]
    REPO_CONFIG.run(selection, "write", scope="project")

    assert permissions(repository) == {"allow": ["Bash(uv sync)"]}
    assert rules(repository) is None
    assert REPO_CONFIG.run(selection, "check", scope="project") == []


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
        REPO_CONFIG.run(selection, "write", scope="project")


def test_run_rejects_an_overlay_rule_naming_a_machine_path(
    repository: Path, published: list[list[str]]
) -> None:
    selection = select(repository, "beta@tj-agents")
    overlay(repository, {"claude_deny": ["Read(C:/Users/name/.ssh/**)"]})

    with pytest.raises(REPO_CONFIG.lock_io.LockError, match="machine-specific path"):
        REPO_CONFIG.run(selection, "write", scope="project")
    assert not (repository / ".claude").exists()


def test_preserved_permissions_need_no_published_lookup(
    repository: Path, published: list[list[str]]
) -> None:
    selection = select(repository, "alpha@tj-agents")
    REPO_CONFIG.run(selection, "write", scope="project")
    committed = permissions(repository)
    committed_rules = rules(repository)
    select(repository, "alpha@tj-agents", "beta@tj-agents")

    assert REPO_CONFIG.run(
        selection, "write", scope="project", preserve_permissions=True
    ) == [
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

    REPO_CONFIG.run(select(repository, "alpha@tj-agents"), "write", scope="project")

    assert {
        path: path.read_bytes() for path in home.rglob("*") if path.is_file()
    } == before
    assert permissions(repository)["deny"] == ["Bash(gh auth login *)"]
