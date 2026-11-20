"""Report exact retirement references without changing files or registrations."""

from pathlib import Path

import plugin_references
import pytest
from claude import plugin_references as claude_references
from codex import plugin_references as codex_references


def test_scan_covers_local_settings_registrations_and_external_worktrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    linked = tmp_path / "linked"
    root.mkdir()
    linked.mkdir()
    (root / ".git").mkdir()
    (linked / ".git").write_text("gitdir: unused", encoding="utf-8")
    files = [
        root / ".agents/plugins.json",
        root / ".claude/settings.local.json",
        root / ".codex/config.toml",
        root / ".agents/repo-capabilities.local.json",
        linked / ".agents/plugins.json",
        tmp_path / "installed_plugins.json",
    ]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"old@marketplace"\n', encoding="utf-8")
    monkeypatch.setattr(plugin_references, "worktrees", lambda root: {root, linked})
    before = {path: path.read_bytes() for path in files}
    findings, problems = plugin_references.scan(
        ["old@marketplace"], [root], [files[-1]]
    )
    assert problems == []
    assert set(findings) == {f"{path}:1: old@marketplace" for path in files}
    assert before == {path: path.read_bytes() for path in files}


def test_missing_roots_and_unreadable_files_are_not_reported_as_clean(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b"\xff")
    findings, problems = plugin_references.scan(
        ["old@marketplace"], [tmp_path / "absent"], [path]
    )
    assert findings == []
    assert len(problems) == 2


def test_a_trusted_container_is_reported_without_recursing_through_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parent = tmp_path / "trusted-parent"
    parent.mkdir()
    (parent / "unrelated.json").write_text('"old@marketplace"', encoding="utf-8")
    repository = parent / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    (repository / "selection.json").write_text('"old@marketplace"', encoding="utf-8")
    for provider in (claude_references, codex_references):
        monkeypatch.setattr(provider, "files", lambda: ())
        monkeypatch.setattr(provider, "projects", lambda: {parent, repository})
    monkeypatch.setattr(plugin_references, "worktrees", lambda root: {root})
    assert plugin_references.audit(["old@marketplace"], []) == 1
    output = capsys.readouterr().out
    assert "unrelated.json" not in output
    assert "selection.json:1" in output
    assert f"requires explicit --root: {parent}" in output
