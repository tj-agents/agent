import json
from pathlib import Path

import pytest
import standards


@pytest.fixture
def python_plugin(tmp_path: Path) -> Path:
    plugin = tmp_path / "python"
    plugin.mkdir()
    for name, kind, applicability in [
        ("style", "convention", "Python projects"),
        ("testing", "contract", "Python tests"),
        ("scaffold", "utility", "new projects"),
    ]:
        directory = plugin / ".agents/python" / kind / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\nkind: {kind}\nprofile: core\n"
            f"applicability: {applicability}\n---\nFull body never included\n",
            encoding="utf-8",
        )
    return plugin


def test_render_limits_routing_to_selected_standards(python_plugin: Path) -> None:
    rendered = standards.render({"python@tj-agents": python_plugin})
    assert "Before the first edit" in rendered
    assert "Python projects: `python:style`" in rendered
    assert "Python tests: `python:testing`" in rendered
    assert "scaffold" not in rendered
    assert "Full body never included" not in rendered
    assert "rust" not in rendered
    assert len(rendered.encode("utf-8")) < 1024


def test_update_preserves_authored_guidance_and_is_idempotent(
    tmp_path: Path, python_plugin: Path
) -> None:
    path = tmp_path / "AGENTS.md"
    original = "# Project\n\nKeep the repository-specific guidance.\n"
    path.write_text(original, encoding="utf-8")
    roots = {"python@tj-agents": python_plugin}
    assert standards.update(tmp_path, roots, check=True)
    assert path.read_text(encoding="utf-8") == original
    assert standards.update(tmp_path, roots)
    assert path.read_text(encoding="utf-8").endswith(original)
    assert not standards.update(tmp_path, roots)
    assert not standards.update(tmp_path, roots, check=True)


def test_update_removes_standards_for_a_deselected_plugin(
    tmp_path: Path, python_plugin: Path
) -> None:
    standards.update(tmp_path, {"python@tj-agents": python_plugin})
    assert standards.update(tmp_path, {})
    assert "python:style" not in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "original",
    [standards.START, standards.END, standards.END + standards.START],
)
def test_document_rejects_broken_markers(original: str) -> None:
    with pytest.raises(standards.StandardsError, match="malformed"):
        standards.document(original, standards.render({}))


def test_read_rejects_a_mismatched_installed_plugin(python_plugin: Path) -> None:
    with pytest.raises(standards.StandardsError, match="Cannot read canonical skills"):
        standards.read(python_plugin, "rust@tj-agents")


def test_read_reports_missing_inventory(tmp_path: Path) -> None:
    with pytest.raises(standards.StandardsError, match="Cannot read canonical skills"):
        standards.read(tmp_path, "python@tj-agents")


def test_always_context_is_projected_and_removed_with_selection(
    tmp_path: Path, python_plugin: Path
) -> None:
    context = "Repository rules loaded from the selected plugin."
    (python_plugin / "tier.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "tier": "python",
                "applies": "always",
                "session_context": context,
            }
        ),
        encoding="utf-8",
    )
    roots = {"python@tj-agents": python_plugin}
    assert standards.update(tmp_path, roots)
    path = tmp_path / "AGENTS.md"
    assert path.read_text(encoding="utf-8").count(context) == 1
    before = path.stat().st_mtime_ns
    assert not standards.update(tmp_path, roots)
    assert path.stat().st_mtime_ns == before
    assert standards.update(tmp_path, {})
    assert context not in path.read_text(encoding="utf-8")


def test_conditional_context_is_left_to_the_runtime_gate(python_plugin: Path) -> None:
    context = "A stack must be detected before this instruction applies."
    (python_plugin / "tier.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "tier": "python",
                "applies": "stack-present",
                "session_context": context,
            }
        ),
        encoding="utf-8",
    )
    assert context not in standards.render({"python@tj-agents": python_plugin})


@pytest.mark.parametrize(
    "context", [None, "", " ", "line\n", "line\rline", "line\u2028"]
)
def test_invalid_always_context_fails_before_changing_instructions(
    python_plugin: Path, tmp_path: Path, context: object
) -> None:
    (python_plugin / "tier.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "tier": "python",
                "applies": "always",
                "session_context": context,
            }
        ),
        encoding="utf-8",
    )
    path = tmp_path / "AGENTS.md"
    path.write_text("Authored instructions.\n", encoding="utf-8")
    with pytest.raises(standards.StandardsError, match="Invalid session context"):
        standards.update(tmp_path, {"python@tj-agents": python_plugin})
    assert path.read_text(encoding="utf-8") == "Authored instructions.\n"
