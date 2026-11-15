import subprocess
from pathlib import Path
from unittest.mock import Mock

import agent_cli
import agent_store
import owned_launch
import pytest
import session_hook
from claude import launch as claude_launch
from tj_agent.host import Host
from tj_cli import process

NATIVE_CURRENT = owned_launch.current


def test_claude_background_tools_drop_inherited_launch_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "environment"
    parent = {
        "TJ_RUN_LAUNCH_ID": "parent-launch",
        "TJ_RUN_PID": "123",
        "TJ_RUN_STARTED_AT": "now",
        "AGENT_CLI_TAB_TITLE": "parent",
        "WT_SESSION": "parent-tab",
        "TMUX_PANE": "%1",
        "CLAUDE_ENV_FILE": str(target),
    }
    monkeypatch.setattr(owned_launch, "for_session", lambda _: None)
    monkeypatch.setattr(session_hook, "worktree", lambda _: None)
    monkeypatch.setattr(agent_store, "run", lambda *_: {"session": {"title": None}})
    monkeypatch.setattr(agent_cli, "host_launch", lambda _: claude_launch)
    session_hook.start(Host.CLAUDE, "child", "/child", parent)
    script = target.read_text(encoding="utf-8")
    for name in parent.keys() - {"CLAUDE_ENV_FILE"}:
        assert f"unset {name}\n" in script
    assert "export CLAUDE_CODE_SESSION_ID=child\n" in script


@pytest.mark.parametrize("host", [Host.CLAUDE, Host.CODEX])
def test_background_thread_cannot_borrow_its_parent_launch(
    monkeypatch: pytest.MonkeyPatch, host: Host
) -> None:
    parent = f"{host}:parent"
    child = f"{host}:child"
    binding = {"session": parent}
    registrations: list[tuple[str, ...]] = []

    def store(*arguments: str) -> dict[str, object]:
        if arguments == ("show", child):
            raise agent_store.AgentStoreError("not-found", "new background thread")
        if arguments == ("show", "--launch-id", "parent-launch"):
            return {
                "launch": {
                    "launch_id": "parent-launch",
                    "host": str(host),
                    "session": binding["session"],
                    "state": "active",
                },
                "session": {
                    "session": parent,
                    "native_id": "parent",
                    "host": str(host),
                    "launch_id": "parent-launch",
                    "state": "active",
                },
            }
        assert arguments[:1] == ("register",)
        registrations.append(arguments)
        borrowed = "--launch-id" in arguments
        if borrowed:
            binding["session"] = child
        return {
            "session": {
                "session": child,
                "title": "parent task" if borrowed else None,
                "launch_id": "parent-launch" if borrowed else None,
            }
        }

    def process_launch(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        assert command == ["tj", "launch"]
        return subprocess.CompletedProcess(command, 0, "parent-launch\n", "")

    adapter = Mock()
    monkeypatch.setattr(owned_launch, "current", NATIVE_CURRENT)
    monkeypatch.setattr(process, "run", process_launch)
    monkeypatch.setattr(agent_store, "run", store)
    monkeypatch.setattr(agent_cli, "host_launch", lambda _host: adapter)
    monkeypatch.setattr(session_hook, "worktree", lambda _cwd: None)
    environment = {
        session_hook.LAUNCH_ENV: "parent-launch",
        session_hook.TITLE_ENV: "parent task",
        session_hook.WRAPPER_PID_ENV: "1234",
        session_hook.WRAPPER_STARTED_AT_ENV: "2026-11-15T00:00:00Z",
        "WT_SESSION": "parent-tab",
        "TMUX_PANE": "%1",
        host.session_variable: "child",
    }

    session_hook.start(host, "child", "/child", environment)

    assert binding["session"] == parent
    assert registrations == [("register", "--session", child, "--cwd", "/child")]
    adapter.name_session.assert_not_called()
    recorded = adapter.record_session_environment.call_args.args[1]
    assert session_hook.LAUNCH_ENV not in recorded
    assert session_hook.TITLE_ENV not in recorded
    assert session_hook.WRAPPER_PID_ENV not in recorded
    assert session_hook.WRAPPER_STARTED_AT_ENV not in recorded
    assert "WT_SESSION" not in recorded
    assert "TMUX_PANE" not in recorded
