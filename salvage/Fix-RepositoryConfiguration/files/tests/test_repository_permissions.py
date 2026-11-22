import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "plugins/agent/.agents/agent/utility/plugins/scripts/repository_permissions.py"
)


def git(root: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / ".agents").mkdir()
    (root / ".agents/plugins.json").write_text(
        json.dumps({"schema_version": 1, "plugins": ["work@tj-agents"]}),
        encoding="utf-8",
    )
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "Initial")
    return root


def verified_root(
    root: Path,
    identity: str,
    claude_allow: list[str],
    codex_prefix_rules: list[dict[str, object]],
) -> Path:
    marketplace, name = identity.rsplit("@", 1)[1], identity.rsplit("@", 1)[0]
    runtime = root / identity.replace("@", "-")
    runtime.mkdir()
    (runtime / "harness.json").write_text(
        json.dumps(
            {
                "plugin": f"{marketplace}/{name}",
                "requires": {
                    "marketplaces": [],
                    "plugins": [],
                    "hooks": [],
                    "permissions": {
                        "claude_allow": claude_allow,
                        "codex_prefix_rules": codex_prefix_rules,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return runtime


def rules() -> list[dict[str, object]]:
    return [
        {
            "pattern": ["Bash(${PROJECT_ROOT}/tools/run.py:*)"],
            "justification": "Run the project tool.",
            "match": ["Bash(${PROJECT_ROOT}/tools/run.py:*)"],
            "not_match": ["Bash(${PROJECT_ROOT}/other.py:*)"],
        }
    ]


def apply(
    project: Path, selected: tuple[str, ...], roots: dict[str, Path]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--root", str(project), "--roots-stdin"],
        input=json.dumps(
            {
                "selected": list(selected),
                "roots": {identity: str(root) for identity, root in roots.items()},
            }
        ),
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def applied(project: Path, selected: tuple[str, ...], roots: dict[str, Path]) -> None:
    result = apply(project, selected, roots)
    assert result.returncode == 0, result.stderr


def test_apply_uses_only_selected_verified_roots_and_keeps_global_configuration(
    project: Path, tmp_path: Path
) -> None:
    global_claude = project / ".claude/settings.json"
    global_codex = project / ".codex/config.toml"
    global_claude.parent.mkdir()
    global_codex.parent.mkdir()
    global_claude.write_text('{"permissions": {"allow": []}}\n', encoding="utf-8")
    global_codex.write_text('model = "kept"\n', encoding="utf-8")
    before = {path: path.read_bytes() for path in (global_claude, global_codex)}
    local = project / ".claude/settings.local.json"
    local.write_text(
        json.dumps({"permissions": {"allow": ["Read(user.txt)"]}}),
        encoding="utf-8",
    )
    work = verified_root(
        tmp_path,
        "work@tj-agents",
        ["Bash(${PROJECT_ROOT}/tools/run.py:*)"],
        rules(),
    )
    personal = verified_root(
        tmp_path,
        "personal@tj-agents",
        ["Read(${PLUGIN_ROOT}/private.txt)"],
        [],
    )

    applied(project, ("work@tj-agents",), {"work@tj-agents": work})

    allow = json.loads(local.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert set(allow) == {
        "Read(user.txt)",
        f"Bash({str(project).replace('\\', '/')}/tools/run.py:*)",
        f"Bash({project}\\tools\\run.py:*)",
    }
    contents = (project / ".codex/rules/repository-permissions.rules").read_text(
        encoding="utf-8"
    )
    assert str(project).replace("\\", "\\\\") in contents
    assert str(personal).replace("\\", "\\\\") not in contents
    assert before == {path: path.read_bytes() for path in before}


def test_apply_expands_project_templates_per_repository_and_is_idempotent(
    project: Path, tmp_path: Path
) -> None:
    second = tmp_path / "second"
    second.mkdir()
    git(second, "init", "--initial-branch=main")
    git(second, "config", "user.name", "Fixture")
    git(second, "config", "user.email", "fixture@example.invalid")
    (second / ".agents").mkdir()
    (second / ".agents/plugins.json").write_bytes(
        (project / ".agents/plugins.json").read_bytes()
    )
    (second / "README.md").write_text("fixture\n", encoding="utf-8")
    git(second, "add", "README.md")
    git(second, "commit", "-m", "Initial")
    runtime = verified_root(
        tmp_path,
        "work@tj-agents",
        ["Bash(${PROJECT_ROOT}/tools/run.py:*)"],
        rules(),
    )
    roots = {"work@tj-agents": runtime}

    applied(project, ("work@tj-agents",), roots)
    first = {
        path: path.read_bytes()
        for path in (
            project / ".claude/settings.local.json",
            project / ".codex/rules/repository-permissions.rules",
            project / ".agents/repository-permissions.local.json",
        )
    }
    applied(project, ("work@tj-agents",), roots)
    assert first == {path: path.read_bytes() for path in first}

    applied(second, ("work@tj-agents",), roots)

    second_allow = json.loads(
        (second / ".claude/settings.local.json").read_text(encoding="utf-8")
    )["permissions"]["allow"]
    assert set(second_allow) == {
        f"Bash({str(second).replace('\\', '/')}/tools/run.py:*)",
        f"Bash({second}\\tools\\run.py:*)",
    }
    assert str(project).replace("\\", "\\\\") not in (
        second / ".codex/rules/repository-permissions.rules"
    ).read_text(encoding="utf-8")


def test_apply_revokes_removed_owned_permissions_and_preserves_user_entries(
    project: Path, tmp_path: Path
) -> None:
    runtime = verified_root(
        tmp_path,
        "work@tj-agents",
        ["Read(${PLUGIN_ROOT}/work.txt)"],
        rules(),
    )
    applied(project, ("work@tj-agents",), {"work@tj-agents": runtime})
    settings = project / ".claude/settings.local.json"
    document = json.loads(settings.read_text(encoding="utf-8"))
    document["permissions"]["allow"].append("Read(user.txt)")
    settings.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    (project / ".agents/plugins.json").unlink()
    applied(project, (), {})

    assert json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"] == [
        "Read(user.txt)"
    ]
    assert not (project / ".codex/rules/repository-permissions.rules").exists()
    assert not (project / ".agents/repository-permissions.local.json").exists()


def test_apply_rejects_changed_owned_rules_before_writing(
    project: Path, tmp_path: Path
) -> None:
    runtime = verified_root(tmp_path, "work@tj-agents", [], rules())
    roots = {"work@tj-agents": runtime}
    applied(project, ("work@tj-agents",), roots)
    rules_path = project / ".codex/rules/repository-permissions.rules"
    rules_path.write_text("changed\n", encoding="utf-8")
    before = rules_path.read_bytes()

    result = apply(project, ("work@tj-agents",), roots)

    assert result.returncode == 1
    assert "owned Codex rules were changed" in result.stderr
    assert rules_path.read_bytes() == before


def test_apply_rejects_hard_linked_local_target_before_writing(
    project: Path, tmp_path: Path
) -> None:
    global_settings = project / ".claude/settings.json"
    global_settings.parent.mkdir()
    global_settings.write_text('{"permissions": {"allow": []}}\n', encoding="utf-8")
    local = project / ".claude/settings.local.json"
    local.hardlink_to(global_settings)
    runtime = verified_root(
        tmp_path,
        "work@tj-agents",
        ["Read(${PLUGIN_ROOT}/work.txt)"],
        [],
    )
    before = global_settings.read_bytes()

    result = apply(project, ("work@tj-agents",), {"work@tj-agents": runtime})

    assert result.returncode == 1
    assert "multiple hard links" in result.stderr
    assert global_settings.read_bytes() == before


def test_claude_only_permissions_preserve_unowned_codex_rules(
    project: Path, tmp_path: Path
) -> None:
    path = project / ".codex/rules/repository-permissions.rules"
    path.parent.mkdir(parents=True)
    path.write_text("User-authored rules.\n", encoding="utf-8")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    runtime = verified_root(tmp_path, "work@tj-agents", ["Read(work.txt)"], [])

    applied(project, ("work@tj-agents",), {"work@tj-agents": runtime})
    (project / ".agents/plugins.json").unlink()
    applied(project, (), {})

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_permissions_reject_another_repositorys_selection(
    project: Path, tmp_path: Path
) -> None:
    runtime = verified_root(tmp_path, "personal@tj-agents", ["Read(private.txt)"], [])

    result = apply(project, ("personal@tj-agents",), {"personal@tj-agents": runtime})

    assert result.returncode == 1
    assert "does not match the target repository" in result.stderr
    assert not (project / ".claude/settings.local.json").exists()


def test_permissions_reject_redirected_parent_before_writing(
    project: Path, tmp_path: Path
) -> None:
    target = project / "other-settings"
    target.mkdir()
    local = target / "settings.local.json"
    local.write_text("{}\n", encoding="utf-8")
    link = project / ".claude"
    if sys.platform == "win32":
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        assert junction.returncode == 0
    else:
        link.symlink_to(target, target_is_directory=True)
    runtime = verified_root(tmp_path, "work@tj-agents", ["Read(work.txt)"], [])

    result = apply(project, ("work@tj-agents",), {"work@tj-agents": runtime})

    assert result.returncode == 1
    assert "redirected permission target" in result.stderr
    assert local.read_text(encoding="utf-8") == "{}\n"
