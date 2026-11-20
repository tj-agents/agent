"""Report exact retirement references without changing files or registrations."""

import subprocess
from pathlib import Path

import plugin_references
import pytest
from claude import plugin_references as claude_references
from codex import plugin_references as codex_references
from tj_cli import process


def test_scan_skips_ignored_builds_but_covers_local_settings_and_external_worktrees(
    tmp_path: Path,
    plugin_source: Path,
) -> None:
    container = tmp_path / "projects"
    container.mkdir()
    root = plugin_source.rename(container / "repo")
    linked = tmp_path / "linked"
    (root / ".gitignore").write_text(
        "build/\n.agents/\n.claude/\n.codex/\nAGENTS.md\nCLAUDE.md\n", encoding="utf-8"
    )
    (root / "tracked.md").write_text('Source\n"old@marketplace"\n', encoding="utf-8")
    (root / "hook.mjs").write_text(
        '"my-old@marketplace"\n"old@marketplace-extra"\n"old@marketplace"\n',
        encoding="utf-8",
    )
    (root / "extensionless-hook").write_text('"old@marketplace"\n', encoding="utf-8")
    (root / "binary.data").write_bytes(b"\0old@marketplace")
    for arguments in (
        [
            "add",
            ".gitignore",
            "tracked.md",
            "hook.mjs",
            "extensionless-hook",
            "binary.data",
        ],
        ["commit", "--quiet", "-m", "Fixture"],
        ["worktree", "add", "--quiet", "--detach", str(linked)],
    ):
        process.run(["git", "-C", str(root), *arguments], check=True, timeout=15)
    files = [
        root / ".agents/plugins.json",
        root / ".claude/settings.json",
        root / ".claude/settings.local.json",
        root / ".codex/config.toml",
        root / ".agents/repo-capabilities.local.json",
        root / "AGENTS.md",
        root / "CLAUDE.md",
        root / "untracked source.json",
        linked / ".agents/plugins.json",
        tmp_path / "installed_plugins.json",
        container / "notes.md",
    ]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"old@marketplace"\n', encoding="utf-8")
    ignored = root / "build/deep/artifact.json"
    ignored.parent.mkdir(parents=True)
    ignored.write_text('"old@marketplace"\n', encoding="utf-8")
    cached = container / "node_modules/cache.json"
    cached.parent.mkdir()
    cached.write_text('"old@marketplace"\n', encoding="utf-8")
    tracked = [root / "tracked.md", linked / "tracked.md"]
    before = {path: path.read_bytes() for path in [*files, *tracked, ignored, cached]}
    findings, problems = plugin_references.scan(
        ["old@marketplace"], [container], [tmp_path / "installed_plugins.json"]
    )
    assert problems == []
    expected = {f"{path}:1: old@marketplace" for path in files}
    expected.update(f"{path}:2: old@marketplace" for path in tracked)
    for project in (root, linked):
        expected.add(f"{project / 'hook.mjs'}:3: old@marketplace")
        expected.add(f"{project / 'extensionless-hook'}:1: old@marketplace")
    assert set(findings) == expected
    assert before == {path: path.read_bytes() for path in before}


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
    plugin_source: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parent = tmp_path / "trusted-parent"
    parent.mkdir()
    (parent / "unrelated.json").write_text('"old@marketplace"', encoding="utf-8")
    repository = plugin_source.rename(parent / "repo")
    (repository / "selection.json").write_text('"old@marketplace"', encoding="utf-8")
    for provider in (claude_references, codex_references):
        monkeypatch.setattr(provider, "files", lambda: ())
        monkeypatch.setattr(provider, "projects", lambda: {parent, repository})
    assert plugin_references.audit(["old@marketplace"], []) == 1
    output = capsys.readouterr().out
    assert "unrelated.json" not in output
    assert "selection.json:1" in output
    assert f"requires explicit --root: {parent}" in output
    assert "Ignored build files are excluded unless tracked" in output
    assert "not a universal disk scan" in output
    assert plugin_references.audit(["old@marketplace"], [parent]) == 1
    explicit = capsys.readouterr().out
    assert "unrelated.json:1" in explicit
    assert "requires explicit --root" not in explicit


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        ("tracked.md", 0),
        ("../outside.md\0", 0),
        ("/outside.md\0", 0),
        ("\0", 0),
        ("tracked.md\0", 1),
    ],
)
def test_invalid_git_inventory_reports_failure_without_scanning_repo_subtrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    returncode: int,
) -> None:
    (tmp_path / ".git").mkdir()
    ignored = tmp_path / "build/artifact.json"
    ignored.parent.mkdir()
    ignored.write_text('"old@marketplace"', encoding="utf-8")
    local = tmp_path / ".agents/plugins.json"
    local.parent.mkdir()
    local.write_text('"old@marketplace"', encoding="utf-8")
    monkeypatch.setattr(plugin_references, "worktrees", lambda root: {root})
    monkeypatch.setattr(
        process,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], returncode, stdout, "inventory failed"
        ),
    )
    findings, problems = plugin_references.scan(["old@marketplace"], [tmp_path], [])
    assert findings == [f"{local}:1: old@marketplace"]
    assert len(problems) == 1
    assert f"Unverified Git inventory for {tmp_path}" in problems[0]


def test_scan_reports_unreadable_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError("root access denied")

    monkeypatch.setattr(Path, "stat", denied)
    findings, problems = plugin_references.scan(["old@marketplace"], [tmp_path], [])
    assert findings == []
    assert len(problems) == 1
    assert "root access denied" in problems[0]
