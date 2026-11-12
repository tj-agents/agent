import importlib
import os
import subprocess
import sys
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
