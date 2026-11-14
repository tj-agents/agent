import hashlib
import json
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path

import jobs
import plugin_update
import pytest
from fake_machine import (
    claude_payload,
    commit_plugin,
    environment,
    git_subdir,
    pin_plugin,
    update_claude_plugin,
    write_json,
)
from tj_agent.host import Host
from tj_cli import process
from tree_digest import Digests

HOSTS = {host.value: host for host in Host}
REAL_RUN = process.run
PYTHON_MANIFEST = '{"name": "python", "skills": "./skills/"}'
NATIVE_INSTALL = ["claude", "plugin", "install"]
NATIVE_UPDATE = ["claude", "plugin", "update"]


def update_plugins(host: str) -> int:
    outcome = plugin_update.update(HOSTS[host], Digests(jobs.state_directory()))
    return 1 if outcome.failures else 0


def install_scopes(home: Path, project: Path, versions: Mapping[str, str]) -> None:
    rows = []
    for scope, revision in versions.items():
        root = home / "plugins/cache/tj-agents/python" / revision[:12]
        claude_payload(root, "python")
        rows.append(
            {
                "scope": scope,
                "projectPath": str(project),
                "gitCommitSha": revision,
                "installPath": str(root),
            }
        )
    write_json(
        home / "plugins/installed_plugins.json", {"plugins": {"python@tj-agents": rows}}
    )


@pytest.mark.parametrize("scope", ["project", "local"])
def test_current_repository_plugins_install_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    home = tmp_path / "claude"
    pin_plugin(home, "python", "a" * 40)
    install_scopes(home, tmp_path, {scope: "a" * 40})
    for name, value in environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(process, "run", run)
    assert update_plugins("claude") == 0
    assert commands == []


@pytest.mark.parametrize("stale_scope", ["user", "project", "local"])
def test_update_changes_only_the_stale_applicable_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_scope: str
) -> None:
    home = tmp_path / "claude"
    pin_plugin(home, "python", "b" * 40)
    install_scopes(
        home,
        tmp_path,
        {
            scope: ("a" if scope == stale_scope else "b") * 40
            for scope in ("user", "project", "local")
        },
    )
    for name, value in environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    registry = home / "plugins/installed_plugins.json"
    document = json.loads(registry.read_text(encoding="utf-8"))
    document["plugins"]["rust@tj-agents"] = [
        {"scope": "project", "projectPath": str(tmp_path / "other")}
    ]
    write_json(registry, document)
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:3] == ["claude", "plugin", "update"]:
            update_claude_plugin(home, command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(process, "run", run)
    assert update_plugins("claude") == 0
    updates = [
        command for command in commands if command[:3] == ["claude", "plugin", "update"]
    ]
    assert updates == [
        [
            "claude",
            "plugin",
            "update",
            "python@tj-agents",
            "--scope",
            stale_scope,
            "--json",
            "-y",
        ]
    ]


def stale_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "claude"
    for plugin in ("python", "rust"):
        pin_plugin(home, plugin, "b" * 40)
    registry = home / "plugins/installed_plugins.json"
    write_json(
        registry,
        {
            "plugins": {
                f"{plugin}@tj-agents": [{"scope": "user", "gitCommitSha": "a" * 40}]
                for plugin in ("python", "rust")
            }
        },
    )
    for name, value in environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return home


def test_distinct_plugins_update_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = stale_plugins(tmp_path, monkeypatch)
    together = threading.Barrier(2, timeout=10)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["claude", "plugin", "update"]:
            together.wait()
            update_claude_plugin(home, command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(process, "run", run)
    assert update_plugins("claude") == 0


def test_an_update_that_misses_its_pin_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = stale_plugins(tmp_path, monkeypatch)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["claude", "plugin", "update"] and "rust" not in command[3]:
            update_claude_plugin(home, command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(process, "run", run)
    assert update_plugins("claude") == 1
    error = capsys.readouterr().err
    assert (
        "claude: rust@tj-agents FAILED: user did not reach its marketplace pin" in error
    )
    assert "python@tj-agents" not in error


@pytest.mark.parametrize("enabled", [True, False])
def test_a_torn_payload_at_its_pin_is_reinstalled_only_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    home = tmp_path / "claude"
    pin_plugin(home, "python", "a" * 40)
    install_scopes(home, tmp_path, {"user": "a" * 40})
    torn = home / "plugins/cache/tj-agents/python" / ("a" * 12)
    (torn / ".claude-plugin/plugin.json").unlink()
    write_json(
        home / "settings.json", {"enabledPlugins": {"python@tj-agents": enabled}}
    )
    for name, value in environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:3] == ["claude", "plugin", "install"]:
            claude_payload(torn, "python")
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(process, "run", run)
    expected = 0 if enabled else 1
    assert update_plugins("claude") == expected
    verb = "install" if enabled else "update"
    assert [command for command in commands if command[:2] == ["claude", "plugin"]] == [
        [
            "claude",
            "plugin",
            verb,
            "python@tj-agents",
            "--scope",
            "user",
            "--json",
            "-y",
        ]
    ]


def behind_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path, manifest: str
) -> tuple[Path, dict[str, str], str]:
    """A user-scope python install one commit behind its git-subdir pin."""
    home = tmp_path / "claude"
    old = commit_plugin(
        source,
        "python",
        {".claude-plugin/plugin.json": PYTHON_MANIFEST, "skills/check/SKILL.md": "old"},
    )
    new = commit_plugin(
        source,
        "python",
        {".claude-plugin/plugin.json": manifest, "skills/check/SKILL.md": "new"},
    )
    write_json(
        home / "plugins/marketplaces/tj-agents/.claude-plugin/marketplace.json",
        {"plugins": [{"name": "python", "source": git_subdir(source, "python", new)}]},
    )
    previous = home / "plugins/cache/tj-agents/python/old-build"
    claude_payload(previous, "python")
    row = {
        "scope": "user",
        "installPath": str(previous),
        "version": "old-build",
        "installedAt": "2026-01-01T00:00:00.000Z",
        "lastUpdated": "2026-01-01T00:00:00.000Z",
        "gitCommitSha": old,
    }
    write_json(
        home / "plugins/installed_plugins.json",
        {"version": 2, "plugins": {"python@tj-agents": [row]}},
    )
    for name, value in environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return home, row, new


def native_commands(monkeypatch: pytest.MonkeyPatch, home: Path) -> list[list[str]]:
    """Record native Claude commands, completing installs; git runs for real."""
    commands: list[list[str]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            return REAL_RUN(command, **options)  # type: ignore[arg-type]
        commands.append(command)
        if command[:3] == ["claude", "plugin", "install"]:
            registry = json.loads(
                (home / "plugins/installed_plugins.json").read_text(encoding="utf-8")
            )
            for row in registry["plugins"][command[3]]:
                claude_payload(Path(row["installPath"]), command[3].partition("@")[0])
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(process, "run", run)
    return commands


def test_a_plugin_behind_its_pin_is_written_and_recorded_like_a_native_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    plugin_source: Path,
) -> None:
    home, row, pin = behind_python(
        tmp_path, monkeypatch, plugin_source, PYTHON_MANIFEST
    )
    commands = native_commands(monkeypatch, home)

    assert update_plugins("claude") == 0

    assert commands == []
    build = f"{pin[:12]}-{hashlib.sha256(b'plugins/python').hexdigest()[:8]}"
    root = home / "plugins/cache/tj-agents/python" / build
    registry = json.loads(
        (home / "plugins/installed_plugins.json").read_text(encoding="utf-8")
    )
    (recorded,) = registry["plugins"]["python@tj-agents"]
    assert recorded["lastUpdated"] != row["lastUpdated"]
    assert recorded == {
        **row,
        "installPath": str(root),
        "version": build,
        "gitCommitSha": pin,
        "lastUpdated": recorded["lastUpdated"],
    }
    assert (root / "skills/check/SKILL.md").read_text(encoding="utf-8") == "new"
    assert Path(row["installPath"]).is_dir()
    assert list((home / "plugins").glob(".tj-update-*")) == []
    assert "claude: python@tj-agents ok (direct)" in capsys.readouterr().out


def test_a_registered_plugin_without_its_payload_is_installed_natively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plugin_source: Path
) -> None:
    home, _row, _pin = behind_python(
        tmp_path, monkeypatch, plugin_source, PYTHON_MANIFEST
    )
    rust = commit_plugin(
        plugin_source, "rust", {".claude-plugin/plugin.json": '{"name": "rust"}'}
    )
    catalog = home / "plugins/marketplaces/tj-agents/.claude-plugin/marketplace.json"
    entries = json.loads(catalog.read_text(encoding="utf-8"))["plugins"]
    entries.append({"name": "rust", "source": git_subdir(plugin_source, "rust", rust)})
    write_json(catalog, {"plugins": entries})
    registry = home / "plugins/installed_plugins.json"
    document = json.loads(registry.read_text(encoding="utf-8"))
    missing = home / "plugins/cache/tj-agents/rust/never-installed"
    document["plugins"]["rust@tj-agents"] = [
        {"scope": "user", "installPath": str(missing), "gitCommitSha": rust}
    ]
    write_json(registry, document)
    write_json(home / "settings.json", {"enabledPlugins": {"rust@tj-agents": True}})
    commands = native_commands(monkeypatch, home)

    assert update_plugins("claude") == 0

    assert commands == [
        [*NATIVE_INSTALL, "rust@tj-agents", "--scope", "user", "--json", "-y"]
    ]
    assert (missing / ".claude-plugin/plugin.json").is_file()


def test_a_direct_update_that_cannot_verify_is_reported_and_left_native(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    plugin_source: Path,
) -> None:
    home, row, pin = behind_python(
        tmp_path, monkeypatch, plugin_source, '{"name": "other"}'
    )
    commands = native_commands(monkeypatch, home)

    outcome = plugin_update.update(Host.CLAUDE, Digests(jobs.state_directory()))

    assert outcome.failures == [
        "claude: python@tj-agents: user did not reach its marketplace pin"
    ]
    assert commands == [
        [*NATIVE_UPDATE, "python@tj-agents", "--scope", "user", "--json", "-y"]
    ]
    assert (
        f"claude: python@tj-agents direct update failed: plugins/python at {pin} "
        "is not a readable plugin named python; using native update"
    ) in capsys.readouterr().err
    registry = json.loads(
        (home / "plugins/installed_plugins.json").read_text(encoding="utf-8")
    )
    assert registry["plugins"]["python@tj-agents"] == [row]
