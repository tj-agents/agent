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
MACHINE_PATHS: Any = importlib.import_module("machine_paths")
RELATIVE = ".codex/config.toml"


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in tuple(os.environ):
        if name == "GIT_CONFIG_COUNT" or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / ".codex").mkdir()
    return tmp_path


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, timeout=15, check=True
    )


def stage(root: Path, text: str) -> None:
    (root / RELATIVE).write_text(text, encoding="utf-8")
    git(root, "add", RELATIVE)


def test_tracked_snapshots_are_empty_for_an_untracked_file(repository: Path) -> None:
    assert MACHINE_PATHS.tracked_snapshots(repository, RELATIVE) == {}


def test_tracked_snapshots_read_the_index_before_the_first_commit(
    repository: Path,
) -> None:
    stage(repository, 'model = "staged"\n')
    assert MACHINE_PATHS.tracked_snapshots(repository, RELATIVE) == {
        "index:0": 'model = "staged"\n'
    }


def test_tracked_snapshots_read_the_index_and_head_separately(
    repository: Path,
) -> None:
    stage(repository, 'model = "committed"\n')
    git(repository, "commit", "--quiet", "-m", "settings")
    stage(repository, 'model = "staged"\n')
    assert MACHINE_PATHS.tracked_snapshots(repository, RELATIVE) == {
        "index:0": 'model = "staged"\n',
        "HEAD": 'model = "committed"\n',
    }


def test_portable_tracked_text_rejects_a_committed_machine_path(
    repository: Path,
) -> None:
    stage(repository, 'path = "C:/Users/fixture/tool"\n')
    git(repository, "commit", "--quiet", "-m", "settings")
    with pytest.raises(MACHINE_PATHS.lock_io.LockError, match=r"\(HEAD\)"):
        MACHINE_PATHS.portable_tracked_text(repository, RELATIVE)


def test_portable_tracked_text_returns_head_when_the_index_matches(
    repository: Path,
) -> None:
    stage(repository, 'model = "committed"\n')
    git(repository, "commit", "--quiet", "-m", "settings")
    assert (
        MACHINE_PATHS.portable_tracked_text(repository, RELATIVE)
        == 'model = "committed"\n'
    )
