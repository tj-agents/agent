from __future__ import annotations

from pathlib import Path

import pytest
from tree_digest import Digests


def tree(root: Path, files: dict[str, bytes]) -> Path:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return root


def test_trees_differing_only_in_markers_caches_and_line_endings_match(
    tmp_path: Path,
) -> None:
    digests = Digests(tmp_path / "state")
    claude = tree(
        tmp_path / "claude",
        {"a.py": b"x\r\ny\r\n", ".in_use/123": b"", "b/__pycache__/a.pyc": b"1"},
    )
    codex = tree(tmp_path / "codex", {"a.py": b"x\ny\n"})
    assert digests.tree(claude) == digests.tree(codex)
    (codex / "a.py").write_bytes(b"changed\n")
    assert digests.tree(claude) != digests.tree(codex)


def test_unchanged_files_are_not_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tree(tmp_path / "plugin", {"a.py": b"x"})
    first = Digests(tmp_path / "state")
    expected = first.tree(root)
    first.save()

    def unreadable(_path: Path) -> bytes:
        raise AssertionError("re-read an unchanged file")

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    assert Digests(tmp_path / "state").tree(root) == expected


def test_a_missing_tree_has_no_digest(tmp_path: Path) -> None:
    assert Digests(tmp_path / "state").tree(tmp_path / "absent") is None
