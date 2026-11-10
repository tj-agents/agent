import io
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import host_plugins
import pytest
import required_plugins
import standards
import tj_prepare
from claude import installed as claude_installed
from claude import project_plugins as claude_projects
from codex import project_plugins as codex_projects
from tj_agent.host import Host
from tj_cli import process


@pytest.fixture
def selected_project(tmp_path: Path) -> Path:
    selection = tmp_path / ".agents/plugins.json"
    selection.parent.mkdir()
    selection.write_text(
        json.dumps({"schema_version": 1, "plugins": ["python@tj-agents"]}),
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.parametrize("host", list(Host))
@pytest.mark.parametrize("local", [False, True])
def test_prepare_installs_missing_selection_before_writing_instructions(
    selected_project: Path, monkeypatch: pytest.MonkeyPatch, host: Host, local: bool
) -> None:
    if host == Host.CLAUDE:
        (selected_project / "AGENTS.override.md").write_text(
            "Codex rules\n", encoding="utf-8"
        )
    if local:
        (selected_project / ".agents/repo-capabilities.local.json").write_text(
            "{}", encoding="utf-8"
        )
    calls: list[str] = []
    plugin = selected_project / "installed"
    plugin.mkdir()
    skill = plugin / ".agents/python/convention/style"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: style\nkind: convention\nprofile: core\n"
        "applicability: Python projects\n---\nFull style body\n",
        encoding="utf-8",
    )

    def configure(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append("configure")
        return subprocess.CompletedProcess([], 0, "", "")

    def inventory(
        directory: Path, selected_host: Host, *, inventory_options: tuple[str, ...]
    ) -> dict[str, Path]:
        assert directory == selected_project
        assert selected_host == host
        assert inventory_options == ()
        calls.append("inventory")
        if "install" not in calls:
            raise required_plugins.MissingRequiredPluginsError(
                "missing", directory, ["python@tj-agents"]
            )
        return {"python@tj-agents": plugin}

    def install(directory: Path, identities: Sequence[str]) -> None:
        assert directory == selected_project
        assert tuple(identities) == ("python@tj-agents",)
        calls.append("install")

    monkeypatch.setattr(process, "run", configure)
    monkeypatch.setattr(required_plugins, "check", inventory)
    monkeypatch.setattr(host_plugins.PLUGINS[host], "install", install)
    if local:
        with pytest.raises(standards.StandardsError, match="Local-only adoption"):
            tj_prepare.prepare(selected_project, host)
        assert calls == ["configure", "inventory", "install", "inventory"]
        assert not (selected_project / "AGENTS.md").exists()
        return
    assert tj_prepare.prepare(selected_project, host)
    assert calls == ["configure", "inventory", "install", "inventory"]
    assert "python:style" in (selected_project / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    assert (selected_project / "CLAUDE.md").read_text(
        encoding="utf-8"
    ) == "@AGENTS.md\n"


def test_prepare_leaves_unadopted_directories_unchanged(tmp_path: Path) -> None:
    assert not tj_prepare.prepare(tmp_path, Host.CLAUDE)
    assert not (tmp_path / "AGENTS.md").exists()


def test_prepare_reports_failure_without_replacing_existing_instructions(
    selected_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = selected_project / "AGENTS.md"
    path.write_text("Existing guidance\n", encoding="utf-8")

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "", "configuration unavailable")

    monkeypatch.setattr(process, "run", fail)
    assert tj_prepare.main(["--host", "claude", "--cwd", str(selected_project)]) == 1
    assert "standards warning: configuration unavailable" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == "Existing guidance\n"


@pytest.mark.parametrize("local", [False, True])
def test_claude_installs_in_the_repository_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local: bool
) -> None:
    if local:
        state = tmp_path / ".agents/repo-capabilities.local.json"
        state.parent.mkdir()
        state.write_text("{}", encoding="utf-8")
    calls: list[tuple[Sequence[str], object]] = []

    def run(
        command: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs.get("cwd")))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(claude_projects.process, "run", run)
    claude_projects.install(tmp_path, ["python@tj-agents"])
    command, directory = calls[0]
    assert directory == tmp_path
    assert command[command.index("--scope") + 1] == ("local" if local else "project")


@pytest.mark.parametrize("returncode", [0, 1])
def test_codex_install_does_not_leave_user_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    config = tmp_path / "config.toml"
    original = 'model = "kept"\n[plugins."unrelated@other"]\nenabled = true\n'
    config.write_text(original, encoding="utf-8")

    def run(
        command: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert kwargs["cwd"] == tmp_path
        config.write_text(
            original + '[plugins."python@tj-agents"]\nenabled = true\n',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, returncode, "", "native failure")

    monkeypatch.setattr(codex_projects.user_plugins, "path", lambda: config)
    monkeypatch.setattr(codex_projects.process, "run", run)
    if returncode:
        with pytest.raises(
            required_plugins.RequiredPluginsError, match="native failure"
        ):
            codex_projects.install(tmp_path, ["python@tj-agents"])
    else:
        codex_projects.install(tmp_path, ["python@tj-agents"])
    assert config.read_text(encoding="utf-8") == original


def test_prepare_skips_host_maintenance_commands(selected_project: Path) -> None:
    assert not tj_prepare.prepare(selected_project, Host.CODEX, ("plugin", "list"))
    assert not (selected_project / "AGENTS.md").exists()


def test_prepare_uses_the_hosts_directory_and_configuration_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "café"
    target.mkdir()
    (target / ".agents").mkdir()
    (target / ".agents/plugins.json").write_text(
        json.dumps({"schema_version": 1, "plugins": ["python@tj-agents"]}),
        encoding="utf-8",
    )

    def configure(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["cwd"] == target
        return subprocess.CompletedProcess([], 0, "", "")

    def inventory(
        directory: Path, host: Host, *, inventory_options: tuple[str, ...]
    ) -> dict[str, Path]:
        assert directory == target
        assert inventory_options == ("--config", 'model="test"')
        return {}

    monkeypatch.setattr(process, "run", configure)
    monkeypatch.setattr(required_plugins, "check", inventory)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(
                json.dumps(
                    [
                        "-C",
                        "café",
                        "--config",
                        'model="test"',
                        "multiline\nprompt & | < > ^ %",
                    ],
                    ensure_ascii=False,
                ).encode("utf-8")
            ),
            encoding="cp1252",
        ),
    )
    assert (
        tj_prepare.main(
            [
                "--host",
                "codex",
                "--cwd",
                str(tmp_path),
                "--arguments-stdin",
            ]
        )
        == 0
    )
    assert (target / "AGENTS.md").is_file()
    assert not (tmp_path / "AGENTS.md").exists()


def test_local_adoption_preserves_the_repositorys_instructions(
    selected_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (selected_project / ".agents/repo-capabilities.local.json").write_text(
        "{}", encoding="utf-8"
    )
    original = "Owner's instructions\n"
    path = selected_project / "AGENTS.md"
    path.write_text(original, encoding="utf-8")

    def configure(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(process, "run", configure)
    monkeypatch.setattr(required_plugins, "check", lambda *args, **kwargs: {})
    with pytest.raises(standards.StandardsError, match="Local-only adoption"):
        tj_prepare.prepare(selected_project, Host.CLAUDE)
    assert path.read_text(encoding="utf-8") == original
    assert not (selected_project / "CLAUDE.md").exists()


def test_claude_pairing_preserves_distinct_existing_instructions(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text("Shared rules\n", encoding="utf-8")
    pointer = tmp_path / "CLAUDE.md"
    pointer.write_text(
        "@AGENTS.md\n\nExtra rules mention `@AGENTS.md`.\n", encoding="utf-8"
    )
    claude_projects.prepare_instructions(tmp_path)
    assert pointer.read_text(encoding="utf-8") == "@AGENTS.md\n"
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == (
        "Shared rules\n\nExtra rules mention `@AGENTS.md`.\n"
    )


def test_codex_reports_an_override_that_would_hide_standards(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.override.md").write_text("Override\n", encoding="utf-8")
    with pytest.raises(standards.StandardsError, match="shadows"):
        codex_projects.prepare_instructions(tmp_path)


def test_claude_refresh_recognizes_project_installs_in_linked_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = tmp_path / "primary"
    common = primary / ".git"
    admin = common / "worktrees/topic"
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = tmp_path / "topic"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    registry = {
        "plugins": {
            "python@tj-agents": [
                {
                    "scope": "project",
                    "projectPath": str(primary),
                    "gitCommitSha": "current",
                    "installPath": str(tmp_path / "cache"),
                }
            ],
            "rust@tj-agents": [
                {
                    "scope": "project",
                    "projectPath": str(tmp_path / "elsewhere"),
                    "gitCommitSha": "other",
                    "installPath": str(tmp_path / "rust"),
                }
            ],
        }
    }
    monkeypatch.chdir(worktree)
    monkeypatch.setattr(claude_installed, "read", lambda path: registry)
    assert claude_installed.installs("tj-agents") == {
        "python": ("current", tmp_path / "cache")
    }


@pytest.mark.parametrize(
    "scopes",
    [
        ["local", "project", "user"],
        ["user", "project", "local"],
        ["project", "user"],
        ["user", "project"],
    ],
)
def test_claude_installed_version_obeys_scope_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scopes: list[str]
) -> None:
    rows = [
        {
            "scope": scope,
            "projectPath": str(tmp_path),
            "gitCommitSha": scope,
            "installPath": str(tmp_path / scope),
        }
        for scope in scopes
    ]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        claude_installed,
        "read",
        lambda path: {
            "plugins": {"python@tj-agents": rows},
        },
    )
    expected = "local" if "local" in scopes else "project"
    assert claude_installed.installs("tj-agents") == {
        "python": (expected, tmp_path / expected),
    }


def test_claude_scope_covers_nested_folders_but_not_nested_repositories(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    assert claude_projects.applies(nested, "project", str(tmp_path))
    (tmp_path / ".git").mkdir()
    (nested / ".git").mkdir()
    assert not claude_projects.applies(nested, "project", str(tmp_path))
