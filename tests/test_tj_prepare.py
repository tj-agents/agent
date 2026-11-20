import io
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import host_plugins
import pytest
import required_plugins
import standards
import tj_prepare
from claude import installed as claude_installed
from claude import plugins as claude_projects
from claude import project_plugins as claude_scope
from codex import plugins as codex_projects
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
def test_prepare_write_installs_missing_selection_before_writing_instructions(
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
        assert not tj_prepare.prepare(selected_project, host, write=True)
        assert sorted(calls[:2]) == ["configure", "inventory"]
        assert calls[2:] == ["install", "inventory"]
        assert not (selected_project / "AGENTS.md").exists()
        return
    assert tj_prepare.prepare(selected_project, host, write=True)
    assert sorted(calls[:2]) == ["configure", "inventory"]
    assert calls[2:] == ["install", "inventory"]
    assert "python:style" in (selected_project / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    assert (selected_project / "CLAUDE.md").read_text(
        encoding="utf-8"
    ) == "@AGENTS.md\n"


def git(directory: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=directory, check=True, capture_output=True)


def tree_snapshot(directory: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(directory).parts
    }


@pytest.mark.parametrize("host", list(Host))
@pytest.mark.parametrize(
    ("linked", "inherited"), [(False, False), (True, False), (True, True)]
)
def test_prepare_never_rewrites_an_authored_checkout(
    tmp_path: Path,
    plugin_source: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: Host,
    linked: bool,
    inherited: bool,
) -> None:
    primary = plugin_source
    (primary / "README.md").write_text("Committed guidance\n", encoding="utf-8")
    git(primary, "add", ".")
    git(primary, "-c", "user.name=t", "-c", "user.email=t", "commit", "-q", "-m", "s")
    project = primary
    if linked:
        project = tmp_path / "linked"
        git(primary, "worktree", "add", "-q", str(project))
    selection_root = primary if inherited else project
    (selection_root / ".agents").mkdir()
    (selection_root / ".agents/plugins.json").write_text(
        '{"schema_version": 1, "plugins": ["python@tj-agents"]}', encoding="utf-8"
    )
    (project / "README.md").write_text("Authored dirtiness\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("Authored Agent guidance\n", encoding="utf-8")
    (project / "CLAUDE.md").write_text("Authored Claude guidance\n", encoding="utf-8")
    plugin = project / "installed"
    skill = plugin / ".agents/python/convention/style"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: style\nkind: convention\nprofile: core\n"
        "applicability: Python projects\n---\nFull style body\n",
        encoding="utf-8",
    )
    modes: list[str] = []

    def configure(_project: Path, mode: str) -> str:
        modes.append(mode)
        return "Drift in: .claude/settings.json"

    monkeypatch.setattr(tj_prepare, "configure", configure)
    monkeypatch.setattr(
        required_plugins,
        "check",
        lambda *_arguments, **_kwargs: {"python@tj-agents": plugin},
    )
    before = tree_snapshot(project)
    primary_before = tree_snapshot(primary)

    assert tj_prepare.prepare(project, host)
    assert tree_snapshot(project) == before
    assert tree_snapshot(primary) == primary_before
    assert modes == ["check"]


def test_prepare_checks_drift_and_reports_its_explicit_owner(
    selected_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (selected_project / "AGENTS.md").write_text(
        "Committed guidance\n", encoding="utf-8"
    )
    (selected_project / "CLAUDE.md").write_text("Committed pointer\n", encoding="utf-8")
    plugin = selected_project / "installed"
    skill = plugin / ".agents/python/convention/style"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: style\nkind: convention\nprofile: core\n"
        "applicability: Python projects\n---\nFull style body\n",
        encoding="utf-8",
    )
    modes: list[str] = []

    def configure(
        command: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        modes.append(command[command.index("--mode") + 1])
        return subprocess.CompletedProcess(
            command, 1, "Drift in: .claude/settings.json\n", ""
        )

    monkeypatch.setattr(process, "run", configure)
    monkeypatch.setattr(
        required_plugins,
        "check",
        lambda directory, host, *, inventory_options: {"python@tj-agents": plugin},
    )
    assert tj_prepare.prepare(selected_project, Host.CLAUDE)
    assert modes == ["check"]
    assert (selected_project / "AGENTS.md").read_text(encoding="utf-8") == (
        "Committed guidance\n"
    )
    assert (selected_project / "CLAUDE.md").read_text(encoding="utf-8") == (
        "Committed pointer\n"
    )
    assert "tj prepare --host claude" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["write", "check"])
def test_configure_keeps_permissions_without_a_published_lookup(
    selected_project: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    commands: list[Sequence[str]] = []

    def configure(
        command: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(process, "run", configure)

    assert tj_prepare.configure(selected_project, mode) == ""
    assert "--preserve-permissions" in commands[0]
    assert commands[0][commands[0].index("--mode") + 1] == mode


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
@pytest.mark.parametrize(
    ("user_setting", "actions"),
    [
        (None, ["install", "disable"]),
        (False, ["install", "disable"]),
        (True, ["install", "disable"]),
    ],
)
def test_claude_installs_once_at_user_scope_leaving_enablement_to_the_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local: bool,
    user_setting: bool | None,
    actions: list[str],
) -> None:
    project = tmp_path / "project"
    if local:
        state = project / ".agents/repo-capabilities.local.json"
        state.parent.mkdir(parents=True)
        state.write_text("{}", encoding="utf-8")
    configuration = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(configuration))
    if user_setting is not None:
        configuration.mkdir()
        (configuration / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"python@tj-agents": user_setting}}),
            encoding="utf-8",
        )
    calls: list[tuple[Sequence[str], object]] = []

    def run(
        command: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs.get("cwd")))
        configuration.mkdir(exist_ok=True)
        (configuration / "settings.json").write_text(
            json.dumps(
                {"enabledPlugins": {"python@tj-agents": command[4] == "install"}}
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(claude_projects.process, "run", run)
    claude_projects.install(project, ["python@tj-agents"])
    assert [command[4] for command, _ in calls] == actions
    for command, directory in calls:
        assert list(command[:4]) == ["claude", "--setting-sources", "user", "plugin"]
        if command[4] == "install":
            assert directory == project
        assert command[5] == "python@tj-agents"
        assert command[command.index("--scope") + 1] == "user"


def test_claude_reports_a_failed_user_scope_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))

    def run(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "native failure")

    monkeypatch.setattr(claude_projects.process, "run", run)
    with pytest.raises(required_plugins.RequiredPluginsError, match="native failure"):
        claude_projects.install(tmp_path, ["python@tj-agents"])


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
    assert not (target / "AGENTS.md").exists()
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
    assert not tj_prepare.prepare(selected_project, Host.CLAUDE)
    assert path.read_text(encoding="utf-8") == original
    assert not (selected_project / "CLAUDE.md").exists()


@pytest.mark.parametrize("host", list(Host))
@pytest.mark.parametrize("write", [False, True])
def test_inherited_local_adoption_preserves_linked_worktree_instructions(
    tmp_path: Path,
    plugin_source: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: Host,
    write: bool,
) -> None:
    primary = plugin_source
    (primary / "README.md").write_text("primary\n", encoding="utf-8")
    git(primary, "add", "README.md")
    git(
        primary,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.test",
        "commit",
        "-qm",
        "initial",
    )
    linked = tmp_path / "linked"
    git(primary, "worktree", "add", "-q", str(linked))
    selection = primary / ".agents/plugins.json"
    selection.parent.mkdir(parents=True)
    selection.write_text(
        json.dumps({"schema_version": 1, "plugins": ["python@tj-agents"]}),
        encoding="utf-8",
    )
    (primary / ".agents/repo-capabilities.local.json").write_text(
        "{}", encoding="utf-8"
    )
    path = linked / "AGENTS.md"
    path.write_text("Owner's instructions\n", encoding="utf-8")

    checked: list[Path] = []

    def inventory(directory: Path, *args: object, **kwargs: object) -> dict[str, Path]:
        checked.append(directory)
        return {}

    monkeypatch.setattr(tj_prepare, "configure", lambda *args: "")
    monkeypatch.setattr(required_plugins, "check", inventory)
    before = tree_snapshot(linked)
    primary_before = tree_snapshot(primary)
    assert not tj_prepare.prepare(linked, host, write=write)
    assert checked == [linked]
    assert tree_snapshot(linked) == before
    assert tree_snapshot(primary) == primary_before


@pytest.mark.parametrize("host", list(Host))
def test_prepare_write_preserves_instructions_when_configuration_creates_local_adoption(
    plugin_source: Path, monkeypatch: pytest.MonkeyPatch, host: Host
) -> None:
    profile = plugin_source.parent / "fixture-claude"
    catalog = profile / "plugins/marketplaces/tj-agents/.claude-plugin/marketplace.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"plugins": [{"name": "python"}]}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    monkeypatch.setenv("CODEX_HOME", str(plugin_source.parent / "fixture-codex"))
    selection = plugin_source / ".agents/plugins.json"
    selection.parent.mkdir()
    selection.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "marketplaces": {"tj-agents": {"repository": "tj-agents/marketplace"}},
                "plugins": ["python@tj-agents"],
            }
        ),
        encoding="utf-8",
    )
    original = b"Owner's instructions\r\n"
    (plugin_source / "AGENTS.md").write_bytes(original)
    (plugin_source / "CLAUDE.md").write_bytes(original)
    monkeypatch.setattr(required_plugins, "check", lambda *args, **kwargs: {})

    assert tj_prepare.prepare(plugin_source, host, write=True)

    assert (plugin_source / ".agents/repo-capabilities.local.json").is_file()
    assert (plugin_source / ".claude/settings.local.json").is_file()
    assert (plugin_source / "AGENTS.md").read_bytes() == original
    assert (plugin_source / "CLAUDE.md").read_bytes() == original


def test_claude_pairing_preserves_distinct_existing_instructions(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text("Shared rules\n", encoding="utf-8")
    pointer = tmp_path / "CLAUDE.md"
    pointer.write_text(
        "@AGENTS.md\n\nExtra rules mention `@AGENTS.md`.\n", encoding="utf-8"
    )
    assert claude_projects.prepare_instructions(tmp_path, write=True)
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
    assert claude_scope.applies(nested, "project", str(tmp_path))
    (tmp_path / ".git").mkdir()
    (nested / ".git").mkdir()
    assert not claude_scope.applies(nested, "project", str(tmp_path))


def test_prepare_does_not_repeat_the_inventory_while_checking_configuration_drift(
    selected_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots: Iterator[dict[str, Path]] = iter([{}])

    def configure(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "Drift in: .claude/settings.json", "")

    def inventory(
        directory: Path, host: Host, *, inventory_options: tuple[str, ...]
    ) -> dict[str, Path]:
        return next(roots)

    observed: list[dict[str, Path]] = []

    def update(project: Path, found: dict[str, Path], *, check: bool) -> bool:
        assert check
        observed.append(found)
        return True

    monkeypatch.setattr(process, "run", configure)
    monkeypatch.setattr(required_plugins, "check", inventory)
    monkeypatch.setattr(standards, "update", update)
    assert tj_prepare.prepare(selected_project, Host.CLAUDE)
    assert observed == [{}]


def test_claude_common_directory_reads_git_metadata_without_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = tmp_path / "primary/.git"
    worktree = common / "worktrees/feature"
    worktree.mkdir(parents=True)
    (worktree / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    (worktree / "commondir").write_text("../..\n", encoding="utf-8")
    checkout = tmp_path / "feature"
    (checkout / "nested").mkdir(parents=True)
    (checkout / ".git").write_text(f"gitdir: {worktree}\n", encoding="utf-8")

    def spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected process")

    monkeypatch.setattr(process, "run", spawn)
    claude_projects.git_common_directory.cache_clear()
    assert claude_projects.git_common_directory(checkout / "nested") == (
        os.path.normcase(str(common.resolve()))
    )
