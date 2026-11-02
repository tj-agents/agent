"""Regression coverage using actual Git stash application conflicts."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/agent/.agents/agent/utility/plugins/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "conflict_adoption", SCRIPTS / "adopt_plugins.py"
)
assert spec is not None and spec.loader is not None
ADOPT: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ADOPT)
FILES = (".agents/plugins.json", ".claude/settings.json", ".codex/config.toml")


@dataclass
class GitScenario:
    root: Path

    def git(
        self, *arguments: str, success: bool = True
    ) -> str | subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        if success:
            assert result.returncode == 0, result.stderr
            return result.stdout
        return result

    def commit(self, title: str) -> None:
        self.git("add", ".")
        self.git("commit", "-m", title)

    def stash_conflict(self, user_key: str | None = None) -> None:
        ADOPT.run(self.root, [], ["python"], "write")
        if user_key:
            self.set_user_key("stashed", user_key)
        self.git("stash", "push", "-m", "Pre-existing plugin configuration")
        ADOPT.run(self.root, [], ["github-actions"], "write")
        if user_key:
            self.set_user_key("upstream", user_key)
        self.commit("Published selection update")
        result = self.git("stash", "apply", success=False)
        assert isinstance(result, subprocess.CompletedProcess)
        assert result.returncode != 0
        unmerged = self.git("diff", "--name-only", "--diff-filter=U")
        assert isinstance(unmerged, str)
        assert ".agents/plugins.json" in unmerged

    def set_user_key(self, value: str, kind: str) -> None:
        if kind == "json":
            path = self.root / ".claude/settings.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["model"] = value
            path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        else:
            path = self.root / ".codex/config.toml"
            text = path.read_text(encoding="utf-8")
            path.write_text(f'model = "{value}"\n' + text, encoding="utf-8")

    def snapshot(self) -> dict[str, bytes]:
        return {name: (self.root / name).read_bytes() for name in FILES}


@pytest.fixture
def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GitScenario:
    for name in tuple(os.environ):
        if name == "GIT_CONFIG_COUNT" or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            monkeypatch.delenv(name)
    monkeypatch.setattr(ADOPT, "current_roster", ADOPT.load_roster)
    result = GitScenario(tmp_path)
    result.git("init", "--initial-branch=main")
    result.git("config", "user.name", "Fixture")
    result.git("config", "user.email", "fixture@example.invalid")
    result.git("config", "core.hooksPath", "")
    result.git("remote", "add", "origin", "https://github.com/tj-agents/fixture.git")
    ADOPT.run(result.root, [], ["base"], "write")
    result.commit("Base selection")
    return result


def test_real_stash_conflicts_converge_both_hosts_preserving_user_edits(
    scenario: GitScenario,
) -> None:
    scenario.stash_conflict()
    index = scenario.git("ls-files", "-u")
    assert isinstance(index, str)
    # Edits made after the failed stash application must survive too.
    path = scenario.root / ".codex/config.toml"
    path.write_text(
        'model = "custom-model"\n' + path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    before = scenario.snapshot()
    assert ADOPT.reconcile(scenario.root, "check")
    assert before == scenario.snapshot()
    ADOPT.reconcile(scenario.root, "write")
    selection = json.loads((scenario.root / FILES[0]).read_text(encoding="utf-8"))[
        "plugins"
    ]
    assert "python@tj-agents" in selection
    assert "github-actions@tj-agents" in selection
    claude = json.loads((scenario.root / FILES[1]).read_text(encoding="utf-8"))
    codex = tomllib.loads((scenario.root / FILES[2]).read_text(encoding="utf-8"))
    assert set(selection) == set(claude["enabledPlugins"])
    assert set(selection) == set(codex["plugins"])
    assert codex["model"] == "custom-model"
    assert index == scenario.git("ls-files", "-u")
    assert ADOPT.reconcile(scenario.root, "write") == []


def test_valid_toml_conflict_example_is_preserved(scenario: GitScenario) -> None:
    path = scenario.root / ".codex/config.toml"
    example = (
        'developer_instructions = """\n'
        '<<<<<<< HEAD\nexample\n=======\nother\n>>>>>>> branch\n"""\n'
    )
    path.write_text(example + path.read_text(encoding="utf-8"), encoding="utf-8")
    ADOPT.reconcile(scenario.root, "write")
    assert (
        tomllib.loads(path.read_text(encoding="utf-8"))["developer_instructions"]
        == tomllib.loads(example)["developer_instructions"]
    )


def test_batch_worktrees_share_exclusions_without_losing_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ADOPT, "current_roster", ADOPT.load_roster)
    root, linked = tmp_path / "first", tmp_path / "linked"
    root.mkdir()
    git = GitScenario(root)
    git.git("init", "--initial-branch=main")
    git.git("config", "user.name", "Fixture")
    git.git("config", "user.email", "fixture@example.invalid")
    git.git("config", "core.hooksPath", "")
    git.git("remote", "add", "origin", "https://github.com/foreign/fixture.git")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    git.commit("Initial fixture")
    git.git("worktree", "add", "-b", "linked", str(linked))
    for checkout in (root, linked):
        (checkout / ".agents").mkdir()
        (checkout / ".agents/plugins.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "marketplaces": {
                        "tj-agents": {"repository": "tj-agents/marketplace"}
                    },
                    "plugins": ["base@tj-agents"],
                }
            ),
            encoding="utf-8",
        )
    exclude = root / ".git/info/exclude"
    exclude.write_text("# User exclusion\nprivate.txt\n", encoding="utf-8")
    before = exclude.read_bytes()
    assert ADOPT.reconcile_many([root, linked], "check")
    assert exclude.read_bytes() == before
    assert not (root / ".codex").exists()
    ADOPT.reconcile_many([root, linked], "write")
    contents = exclude.read_text(encoding="utf-8")
    assert exclude.read_bytes().startswith(before)
    assert contents.count(ADOPT.repo_config.EXCLUDE_START) == 2
    assert ADOPT.reconcile_many([root, linked], "write") == []


def test_real_conflict_inside_parseable_toml_string_is_not_accepted(
    scenario: GitScenario,
) -> None:
    path = scenario.root / ".codex/config.toml"
    original = path.read_text(encoding="utf-8")
    path.write_text(
        'developer_instructions = """\nbase\n"""\n' + original, encoding="utf-8"
    )
    scenario.commit("Instruction base")
    path.write_text(
        'developer_instructions = """\nstashed\n"""\n' + original, encoding="utf-8"
    )
    scenario.git("stash", "push")
    path.write_text(
        'developer_instructions = """\nupstream\n"""\n' + original, encoding="utf-8"
    )
    scenario.commit("Upstream instructions")
    result = scenario.git("stash", "apply", success=False)
    assert isinstance(result, subprocess.CompletedProcess) and result.returncode != 0
    assert (
        "<<<<<<<"
        in tomllib.loads(path.read_text(encoding="utf-8"))["developer_instructions"]
    )
    before = scenario.snapshot()
    with pytest.raises((ValueError, ADOPT.lock_io.LockError)):
        ADOPT.reconcile(scenario.root, "write")
    assert scenario.snapshot() == before


@pytest.mark.parametrize("kind", ["json", "toml"])
def test_conflicting_unmanaged_settings_block_the_entire_write(
    scenario: GitScenario, kind: str
) -> None:
    scenario.stash_conflict(user_key=kind)
    before = scenario.snapshot()
    with pytest.raises((ADOPT.lock_io.LockError, ValueError)):
        ADOPT.reconcile(scenario.root, "write")
    assert before == scenario.snapshot()
    scenario.git("reset", "--hard", "HEAD~1")


def test_independent_marketplace_addition_is_merged_with_plugin_membership(
    scenario: GitScenario,
) -> None:
    ADOPT.run(
        scenario.root,
        [],
        ["legacy@concertable"],
        "write",
        marketplaces=["concertable=Concertable/agents"],
    )
    scenario.git("stash", "push")
    ADOPT.run(scenario.root, [], ["python"], "write")
    scenario.commit("Independent plugin update")
    result = scenario.git("stash", "apply", success=False)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode != 0
    ADOPT.reconcile(scenario.root, "write")
    document = json.loads((scenario.root / FILES[0]).read_text(encoding="utf-8"))
    assert "legacy@concertable" in document["plugins"]
    assert "python@tj-agents" in document["plugins"]


def test_removal_of_last_marketplace_plugin_does_not_drop_an_independent_addition(
    scenario: GitScenario,
) -> None:
    ADOPT.run(
        scenario.root,
        [],
        ["legacy@concertable"],
        "write",
        marketplaces=["concertable=Concertable/agents"],
    )
    scenario.commit("Foreign plugin baseline")
    ADOPT.run(scenario.root, [], [], "write", removals=["legacy@concertable"])
    scenario.git("stash", "push")
    ADOPT.run(scenario.root, [], ["new@concertable"], "write")
    scenario.commit("New plugin in the same marketplace")
    result = scenario.git("stash", "apply", success=False)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode != 0
    ADOPT.reconcile(scenario.root, "write")
    document = json.loads((scenario.root / FILES[0]).read_text(encoding="utf-8"))
    assert "legacy@concertable" not in document["plugins"]
    assert "new@concertable" in document["plugins"]


def test_committed_markers_have_no_authority_to_reconstruct_configuration(
    scenario: GitScenario,
) -> None:
    scenario.stash_conflict()
    scenario.git("add", *FILES)
    scenario.git("commit", "-m", "Invalid committed conflict markers")
    before = scenario.snapshot()
    with pytest.raises(ADOPT.lock_io.LockError, match="no complete Git merge stages"):
        ADOPT.reconcile(scenario.root, "write")
    assert before == scenario.snapshot()


def test_invalid_unmanaged_toml_is_rejected_before_any_write(
    scenario: GitScenario,
) -> None:
    scenario.stash_conflict()
    path = scenario.root / ".codex/config.toml"
    path.write_text("broken = [\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    before = scenario.snapshot()
    with pytest.raises(ValueError):
        ADOPT.reconcile(scenario.root, "write")
    assert before == scenario.snapshot()
