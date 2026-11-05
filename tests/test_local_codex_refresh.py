from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/agent/.agents/agent/utility/plugins/scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "local_settings_adoption", SCRIPTS / "adopt_plugins.py"
)
assert SPEC is not None and SPEC.loader is not None
ADOPT: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADOPT)
LOCAL_SETTINGS = (
    "[shell_environment_policy.set]\n"
    'GH_CONFIG_DIR = "C:/Users/Fixture/bot"\n'
    'GIT_CONFIG_VALUE_2 = "!/home/fixture/bot/credential-helper"\n'
    'PATH = "C:/Users/Fixture/bin"\n'
)


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=True,
    )
    return result.stdout


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in tuple(os.environ):
        if name == "GIT_CONFIG_COUNT" or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(ADOPT, "current_roster", ADOPT.load_roster)
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "core.hooksPath", "")
    git(tmp_path, "remote", "add", "origin", "https://github.com/tj-agents/fixture.git")
    ADOPT.run(tmp_path, [], ["base"], "write")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Portable project settings")
    return tmp_path


def test_refresh_preserves_unstaged_local_codex_environment(project: Path) -> None:
    path = project / ".codex/config.toml"
    portable = git(project, "show", ":.codex/config.toml")
    path.write_text(
        LOCAL_SETTINGS + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    index = git(project, "ls-files", "--stage")

    ADOPT.run(project, [], ["python"], "write")

    assert LOCAL_SETTINGS in path.read_text(encoding="utf-8")
    assert "python@tj-agents" in path.read_text(encoding="utf-8")
    assert git(project, "show", ":.codex/config.toml") == portable
    assert git(project, "ls-files", "--stage") == index
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert ADOPT.reconcile(project, "write") == []
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("commit", [False, True])
def test_refresh_rejects_indexed_machine_paths(project: Path, commit: bool) -> None:
    path = project / ".codex/config.toml"
    portable = path.read_text(encoding="utf-8")
    path.write_text(LOCAL_SETTINGS + "\n" + portable, encoding="utf-8")
    git(project, "add", ".codex/config.toml")
    if commit:
        git(project, "commit", "-m", "Machine-specific project settings")
    path.write_text(portable, encoding="utf-8")
    if commit:
        git(project, "add", ".codex/config.toml")
    before = path.read_bytes()

    with pytest.raises(ADOPT.lock_io.LockError, match="machine-specific paths"):
        ADOPT.reconcile(project, "write")

    assert path.read_bytes() == before


@pytest.mark.parametrize("git_checkout", [False, True])
def test_refresh_rejects_machine_paths_without_tracked_config(
    project: Path, git_checkout: bool
) -> None:
    if git_checkout:
        git(project, "rm", "--cached", ".codex/config.toml")
        root = project
    else:
        root = project.parent / "unversioned-project"
        ADOPT.run(root, [], ["base"], "write")
    path = root / ".codex/config.toml"
    path.write_text(
        LOCAL_SETTINGS + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    with pytest.raises(ADOPT.lock_io.LockError, match="machine-specific paths"):
        ADOPT.reconcile(root, "write")


def test_refresh_rejects_new_generated_machine_paths(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = project / ".codex/config.toml"
    path.write_text(
        LOCAL_SETTINGS + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    original = ADOPT.repo_config.codex_block
    monkeypatch.setattr(
        ADOPT.repo_config,
        "codex_block",
        lambda *args: original(*args) + '\n[new_setting]\npath = "/home/fixture/new"\n',
    )
    before = path.read_bytes()

    with pytest.raises(ADOPT.lock_io.LockError, match="machine-specific paths"):
        ADOPT.reconcile(project, "write")

    assert path.read_bytes() == before


def test_refresh_rejects_generated_permission_paths(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = project / ".codex/config.toml"
    path.write_text(
        LOCAL_SETTINGS + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    permissions = ADOPT.repo_config.host_permissions
    original = permissions.codex_settings
    monkeypatch.setattr(
        permissions,
        "codex_settings",
        lambda text, mode: (
            'generated_path = "/home/fixture/generated"\n' + original(text, mode)
        ),
    )

    with pytest.raises(ADOPT.lock_io.LockError, match="machine-specific paths"):
        ADOPT.reconcile(project, "write")


def test_refresh_rejects_machine_paths_in_unborn_checkout(project: Path) -> None:
    git(project, "update-ref", "-d", "refs/heads/main")
    path = project / ".codex/config.toml"
    path.write_text(
        LOCAL_SETTINGS + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    with pytest.raises(ADOPT.lock_io.LockError, match="machine-specific paths"):
        ADOPT.reconcile(project, "write")
