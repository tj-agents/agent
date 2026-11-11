from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, TracebackType
from typing import cast

import agent_cli
import agent_store
import host_permissions
import host_plugins
import owned_launch
import pytest
import session_hook
from claude import installed as claude_installed
from codex import installed as codex_installed
from codex import launch as codex_launch
from fake_machine import CodexPlugins
from tj_agent import worktrees
from tj_agent.host import Host

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=list(Host), ids=str)
def host(request: pytest.FixtureRequest) -> Host:
    return cast(Host, request.param)


@pytest.fixture
def adapter(host: Host) -> ModuleType:
    return cast(ModuleType, agent_cli.host_launch(host))


@dataclass
class Server:
    replies: dict[str, object] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def request(self, method: str, parameters: dict[str, object]) -> object:
        self.calls.append((method, parameters))
        return self.replies.get(method, {})

    def __enter__(self) -> Server:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass


def test_launch_preserves_prompt_model_and_effort(
    adapter: ModuleType, tmp_path: Path
) -> None:
    prompt = 'Read "instructions"; preserve $values and Unicode: \u03bb'
    arguments = adapter.start_arguments(
        tmp_path, "parity", prompt, "fixture-model", "high"
    )
    assert arguments[-1] == prompt
    assert arguments[arguments.index("--model") + 1] == "fixture-model"
    assert "high" in arguments or "model_reasoning_effort=high" in arguments


def test_handoff_preserves_item_parent_and_standing_rule(
    host: Host,
    adapter: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = ROOT / f".agents/agent/resources/handoff/launch_{host}.py"
    spec = importlib.util.spec_from_file_location(f"parity_handoff_{host}", path)
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher, "_load_agent_cli", lambda: agent_cli)
    monkeypatch.setattr(
        adapter, "resolve_executable", lambda **kwargs: ("fixture-agent", {})
    )
    monkeypatch.setattr(
        agent_store,
        "run",
        lambda *arguments: {"session": {"title": "parent"}},
    )
    calls: list[tuple[Host, list[str], dict[str, str]]] = []

    def launch(
        selected: Host,
        directory: Path,
        title: str,
        arguments: list[str],
        **options: str,
    ) -> dict[str, dict[str, str]]:
        assert directory == tmp_path and title == "parity"
        calls.append((selected, arguments, options))
        return {"launch": {"launch_id": "fixture"}}

    monkeypatch.setattr(agent_cli, "launch", launch)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Work on the fixture.", encoding="utf-8")
    assert (
        launcher.main(
            [
                "--working-directory",
                str(tmp_path),
                "--prompt-path",
                str(prompt),
                "--title",
                "parity",
                "--lane",
                "L4",
                "--item",
                "130",
                "--parent",
                "claude:parent",
            ]
        )
        == 0
    )
    selected, arguments, options = calls.pop()
    assert selected is host
    assert options == {"item": "130", "parent": "claude:parent"}
    assert str(prompt) in arguments[-1] and "Standing rule:" in arguments[-1]


def test_session_start_binds_the_launch(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    claims: list[tuple[Host, str, Path, Mapping[str, str]]] = []

    def capture(
        selected: Host, native_id: str, root: Path, environ: Mapping[str, str]
    ) -> None:
        claims.append((selected, native_id, root, environ))

    monkeypatch.setattr(worktrees, "claim_started_worktree", capture)

    def store(*arguments: str) -> dict[str, dict[str, None]]:
        calls.append(arguments)
        return {"session": {"title": None}}

    monkeypatch.setattr(agent_store, "run", store)
    monkeypatch.setattr(session_hook, "worktree", lambda cwd: "/fixture")
    monkeypatch.setattr(owned_launch, "current", lambda: "launch")
    session_hook.start(host, "native", "/fixture/sub", {"TJ_RUN_LAUNCH_ID": "launch"})
    assert claims == [
        (host, "native", Path("/fixture"), {"TJ_RUN_LAUNCH_ID": "launch"})
    ]
    assert calls == [
        (
            "register",
            "--session",
            f"{host}:native",
            "--cwd",
            "/fixture/sub",
            "--worktree",
            "/fixture",
            "--launch-id",
            "launch",
        )
    ]


def test_session_title_reaches_the_native_host(
    host: Host, adapter: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if host is Host.CLAUDE:
        assert adapter.name_session("native", "parity title") is None
        arguments = adapter.start_arguments(
            tmp_path, "parity title", "prompt", None, None
        )
        assert arguments[arguments.index("--name") + 1] == "parity title"
    else:
        server = Server()
        monkeypatch.setattr(adapter, "daemon", lambda: server)
        adapter.name_session("native", "parity title")
        assert server.calls == [
            ("thread/name/set", {"threadId": "native", "name": "parity title"})
        ]


def test_native_liveness_reports_active_sessions(
    host: Host, adapter: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter, "executable", lambda: "fixture-agent")
    listing = [{"sessionId": "native", "pid": 123, "status": "busy"}]

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, json.dumps(listing), "")

    if host is Host.CODEX:
        server = Server(
            {
                "thread/loaded/list": {"data": ["native"], "nextCursor": None},
                "thread/read": {"thread": {"status": {"type": "active"}}},
            }
        )
        monkeypatch.setattr(adapter, "daemon", lambda: server)
        live = adapter.liveness()
    else:
        live = adapter.liveness(run=run)
    assert live["native"].alive
    assert live["native"].activity in ("busy", "active")


@dataclass
class Report:
    listing: object
    commands: list[list[str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    inventory: CodexPlugins | None = None

    def step(
        self, host: Host, label: str, command: list[str]
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if self.inventory is not None and command[:3] == ["codex", "plugin", "add"]:
            self.inventory.install(command[3])
        output = (
            self.inventory.listing()
            if self.inventory is not None
            else json.dumps(self.listing)
            if label in ("list", "versions", "installed versions")
            else "{}"
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    def fail(self, host: Host, label: str, detail: str) -> None:
        raise AssertionError((host, label, detail))


def test_refresh_updates_selected_plugins(
    host: Host, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    row = {"id": "agent@tj-agents", "scope": "user"}
    report = Report([row])
    installed = claude_installed
    if host is Host.CODEX:
        installed = codex_installed
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
        monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(tmp_path / "state"))
        report.inventory = CodexPlugins(
            tmp_path / "codex", {"agent@tj-agents": "local"}
        )
        report.listing = {
            "installed": [
                {"pluginId": "agent@tj-agents", "marketplaceName": "tj-agents"}
            ]
        }
        monkeypatch.setattr(installed, "cached", lambda marketplaces: set())
        monkeypatch.setattr(installed, "read_user_config", lambda: "")
        monkeypatch.setattr(installed, "keep_project_scope", lambda *args: None)
        monkeypatch.setattr(installed, "trust_hooks", lambda report: None)
        monkeypatch.setattr(codex_launch, "liveness", dict)
    installed.refresh(report, ("tj-agents",), lambda *args: False)
    assert any("agent@tj-agents" in command for command in report.commands)
    assert all(command[0] == str(host) for command in report.commands)


def test_plugin_inventory_reads_native_rows(host: Host) -> None:
    rows = [{"name": "agent"}]
    native: object = rows if host is Host.CLAUDE else {"installed": rows}
    assert host_plugins.PLUGINS[host].inventory_entries(native) == rows


def test_permissions_preserve_user_settings(host: Host) -> None:
    if host is Host.CLAUDE:
        rendered = host_permissions.claude_settings('{"userKey": 42}', "standard")
        config = json.loads(rendered)
        assert config["permissions"]["defaultMode"] == "default"
    else:
        rendered = host_permissions.codex_settings("userKey = 42\n", "standard")
        config = tomllib.loads(rendered)
        assert config["approval_policy"] == "on-request"
        assert config["default_permissions"] == ":workspace"
    assert config["userKey"] == 42


def test_resume_preserves_the_native_session(
    host: Host, adapter: ModuleType, tmp_path: Path
) -> None:
    arguments = adapter.resume_arguments(tmp_path, "parity", "native-session")
    assert (
        arguments[arguments.index("--resume" if host is Host.CLAUDE else "resume") + 1]
        == "native-session"
    )


def test_native_executable_selection(
    host: Host, adapter: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = (
        tmp_path
        / ".local/bin"
        / ("claude.exe" if sys.platform == "win32" else "claude")
    )
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\x7fELFfixture")
    if host is Host.CLAUDE:
        assert adapter.resolve_executable(
            home=tmp_path, which=lambda name: None
        ) == str(binary)
    else:
        monkeypatch.setattr(
            adapter.process,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                [], 0, "codex-cli 1.2.3", ""
            ),
        )
        resolved, version = adapter.resolve_executable(
            minimum="1.0.0", candidates=[str(binary)]
        )
        assert resolved == str(binary) and version["text"] == "1.2.3"


def test_session_environment_reaches_tool_commands(
    host: Host, adapter: ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "environment file"
    adapter.record_session_environment(
        "native with spaces", {"CLAUDE_ENV_FILE": str(target)}
    )
    if host is Host.CLAUDE:
        assert (
            target.read_text(encoding="utf-8")
            == "export CLAUDE_CODE_SESSION_ID='native with spaces'\n"
        )
    else:
        assert not target.exists()
