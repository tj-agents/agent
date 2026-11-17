import json
import shutil
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import agent_cli
import agent_store
import pytest
import session_resume
from claude import launch as claude_launch
from launch_contract import PreparedLaunch
from launch_errors import LaunchError
from tj_agent.host import Host
from tj_cli import process

THREAD = "123e4567-e89b-12d3-a456-426614174000"


@pytest.mark.parametrize("registered", [None, "old"])
def test_predecessor_preserves_the_exact_recorded_binding(
    monkeypatch: pytest.MonkeyPatch, registered: str | None
) -> None:
    monkeypatch.setattr(
        agent_store,
        "run",
        lambda *args: {
            "session": {"session": f"codex:{THREAD}", "launch_id": registered}
        },
    )
    assert session_resume.predecessor(f"codex:{THREAD}") == registered
    if registered is None:
        with pytest.raises(LaunchError, match="rebound"):
            session_resume.predecessor(f"codex:{THREAD}", "old")


@pytest.mark.parametrize("code", ["not-found", "storage"])
def test_only_an_unregistered_native_session_can_omit_a_predecessor(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    def store(*args: str) -> dict[str, object]:
        raise agent_store.AgentStoreError(code, "fixture error")

    monkeypatch.setattr(agent_store, "run", store)
    if code == "not-found":
        assert session_resume.predecessor(f"codex:{THREAD}") is None
    else:
        with pytest.raises(LaunchError, match="Cannot inspect"):
            session_resume.predecessor(f"codex:{THREAD}")
    with pytest.raises(LaunchError, match="Cannot inspect"):
        session_resume.predecessor(f"codex:{THREAD}", "old")


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"session": "codex:other", "launch_id": "old"},
        {"session": f"codex:{THREAD}", "launch_id": "new"},
    ],
)
def test_admission_rejects_a_different_owner(
    monkeypatch: pytest.MonkeyPatch, reply: dict[str, object]
) -> None:
    monkeypatch.setattr(
        process,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, json.dumps(reply), ""
        ),
    )
    with pytest.raises(LaunchError, match="different owner"):
        session_resume.check(f"codex:{THREAD}", "old")


def test_unknown_or_live_predecessor_refuses_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        process,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 2, "", "native liveness is unknown"
        ),
    )
    with pytest.raises(LaunchError, match="liveness is unknown"):
        session_resume.check(f"codex:{THREAD}", "old")


@pytest.mark.parametrize("host", list(Host))
def test_resume_admission_precedes_tab_open_and_carries_the_exact_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Host
) -> None:
    events: list[object] = []
    handle = f"{host}:{THREAD}"
    adapter = SimpleNamespace(
        SESSION_ENVIRONMENT=(),
        TAB_CLEARED=(),
        TAB_FORCED={},
        prepare=lambda *args: PreparedLaunch(
            (f"native-{host}", "resume", THREAD), handle
        ),
        confirm=lambda *args: events.append("confirmed"),
        abandon=lambda *args: events.append("abandoned"),
    )
    monkeypatch.setattr(agent_cli, "host_launch", lambda _: adapter)
    monkeypatch.setattr(shutil, "which", lambda _: "tj")
    monkeypatch.setattr(session_resume, "predecessor", lambda session, expected: "old")
    monkeypatch.setattr(session_resume, "check", lambda *args: events.append(args))
    monkeypatch.setattr(
        agent_cli, "open_tab", lambda *args, **kwargs: events.append(args[2])
    )
    monkeypatch.setattr(uuid, "uuid4", lambda: SimpleNamespace(hex="new"))
    monkeypatch.setattr(
        agent_cli,
        "await_launch",
        lambda *args, **kwargs: {
            "launch": {"state": "active"},
            "session": {"session": handle, "launch_id": "new"},
        },
    )
    call_launch = cast(Callable[..., object], agent_cli.launch)

    call_launch(host, tmp_path, "Recovered", [], resume_from="old")

    assert events[0] == (handle, "old")
    command = cast(list[str], events[1])
    assert command[command.index("--session") + 1] == handle
    assert command[command.index("--resume-from") + 1] == "old"
    assert events[-1] == "confirmed"
    assert "abandoned" not in events


def test_claude_resume_prepares_an_exact_native_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_launch, "executable", lambda: "native-claude")
    arguments = ["--resume", THREAD, "--name", "Existing"]
    prepared = claude_launch.prepare(tmp_path, "Existing", arguments, "tj", {})
    assert prepared.session == f"claude:{THREAD}"
    assert prepared.command == ("native-claude", *arguments)
    assert not prepared.created
