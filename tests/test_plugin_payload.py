import json
from pathlib import Path

import plugin_payload
import pytest
from fake_machine import commit_plugin, git_subdir


def test_export_checks_out_only_the_pinned_subtree(
    plugin_source: Path, tmp_path: Path
) -> None:
    pinned = commit_plugin(
        plugin_source,
        "python",
        {".claude-plugin/plugin.json": '{"name": "python"}', "skills/a.md": "pinned"},
    )
    commit_plugin(plugin_source, "python", {"skills/a.md": "newer"})
    commit_plugin(plugin_source, "rust", {".claude-plugin/plugin.json": "{}"})
    source = plugin_payload.Source.parse(git_subdir(plugin_source, "python", pinned))
    assert source is not None

    exported = plugin_payload.export({"python@tj-agents": source}, tmp_path / "stage")

    assert exported.problems == {}
    root = exported.payloads["python@tj-agents"]
    assert sorted(path.relative_to(root).as_posix() for path in root.rglob("*")) == [
        ".claude-plugin",
        ".claude-plugin/plugin.json",
        "skills",
        "skills/a.md",
    ]
    assert (root / "skills/a.md").read_text(encoding="utf-8") == "pinned"
    manifest = json.loads((root / ".claude-plugin/plugin.json").read_text("utf-8"))
    assert manifest == {"name": "python"}


def test_export_reports_an_unavailable_commit_for_each_plugin_it_pins(
    plugin_source: Path, tmp_path: Path
) -> None:
    commit_plugin(plugin_source, "python", {"a.md": "a"})
    missing = plugin_payload.Source(str(plugin_source), "plugins/python", "f" * 40)

    exported = plugin_payload.export(
        {"python@tj-agents": missing, "python@agents": missing}, tmp_path / "stage"
    )

    assert exported.payloads == {}
    assert sorted(exported.problems) == ["python@agents", "python@tj-agents"]
    assert all("git fetch failed" in problem for problem in exported.problems.values())


@pytest.mark.parametrize(
    "source",
    [
        {"source": "local", "path": "./plugins/python"},
        {"source": "git-subdir", "url": "u", "path": "./plugins/python", "sha": "main"},
        {"source": "git-subdir", "url": "u", "path": "../outside", "sha": "a" * 40},
        {"source": "git-subdir", "url": "u", "path": "./", "sha": "a" * 40},
        {"source": "git-subdir", "path": "./plugins/python", "sha": "a" * 40},
    ],
)
def test_source_accepts_only_a_git_subdir_pinned_to_a_commit(
    source: dict[str, str],
) -> None:
    assert plugin_payload.Source.parse(source) is None
