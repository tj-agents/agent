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
import session_hook
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


@pytest.mark.parametrize("prior", [None, "new"])
def test_started_session_without_a_different_owner_needs_no_transfer(
    monkeypatch: pytest.MonkeyPatch,
    prior: str | None,
) -> None:
    monkeypatch.setattr(session_resume, "predecessor", lambda _: prior)
    monkeypatch.setattr(
        process, "run", lambda *args, **kwargs: pytest.fail("unexpected transfer")
    )
    assert session_resume.bind_started(f"claude:{THREAD}", "new") is None


@pytest.mark.parametrize("admitted", [True, False])
@pytest.mark.parametrize("host", list(Host))
def test_public_resume_binds_before_claiming_and_never_reregisters(
    monkeypatch: pytest.MonkeyPatch,
    admitted: bool,
    host: Host,
) -> None:
    handle = f"{host}:{THREAD}"
    current = {"session": handle, "launch_id": "new", "state": "active"}
    events: list[object] = []

    def store(*args: str) -> dict[str, object]:
        assert args == ("show", handle)
        return {"session": {"session": handle, "launch_id": "old"}}

    def execute(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert command == [
            "tj",
            "_resume-bind",
            "--session",
            handle,
            "--launch-id",
            "new",
            "--from-launch-id",
            "old",
        ]
        events.append("admission")
        return subprocess.CompletedProcess(
            command,
            0 if admitted else 1,
            json.dumps({"session": current}),
            "predecessor is live",
        )

    adapter = SimpleNamespace(
        record_session_environment=lambda *args, **kwargs: events.append("environment")
    )
    monkeypatch.setattr(agent_store, "run", store)
    monkeypatch.setattr(process, "run", execute)
    monkeypatch.setattr(session_hook.owned_launch, "for_session", lambda _: "new")
    monkeypatch.setattr(
        session_hook.codex_launch,
        "registered_session",
        lambda _: {"session": handle, "launch_id": "old", "state": "active"},
    )
    monkeypatch.setattr(
        session_hook.codex_processes,
        "record",
        lambda session, launch: events.append((session, launch)),
    )
    monkeypatch.setattr(session_hook, "worktree", lambda _: "/repo")
    monkeypatch.setattr(agent_cli, "host_launch", lambda _: adapter)
    monkeypatch.setattr(
        session_hook.worktrees,
        "claim_started_worktree",
        lambda *args: events.append("claim"),
    )
    if admitted:
        assert (
            session_hook.start(
                host, THREAD, "/repo", {"TJ_RUN_LAUNCH_ID": "new"}
            )
            == current
        )
        expected = ["admission", "environment", "claim"]
    else:
        with pytest.raises(LaunchError, match="predecessor is live"):
            session_hook.start(
                host, THREAD, "/repo", {"TJ_RUN_LAUNCH_ID": "new"}
            )
        expected = ["admission"]
    assert events == ([(THREAD, "new")] if host is Host.CODEX else []) + expected


def test_native_inventory_keeps_every_live_process_for_one_conversation() -> None:
    rows = [
        {"sessionId": THREAD, "pid": 42},
        {"sessionId": THREAD, "pid": 43},
        {"sessionId": THREAD},
    ]
    inventory = claude_launch.liveness(
        run=lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, json.dumps(rows), ""
        ),
        strict=True,
    )
    assert inventory[THREAD].alive
    assert inventory[THREAD].process_ids == (42, 43)


@pytest.mark.parametrize("pid", [0, -1, True, "42", None])
def test_native_inventory_rejects_unverifiable_processes(pid: object) -> None:
    with pytest.raises(LaunchError, match="invalid process id"):
        claude_launch.liveness(
            run=lambda *args, **kwargs: subprocess.CompletedProcess(
                [], 0, json.dumps([{"sessionId": THREAD, "pid": pid}]), ""
            ),
            strict=True,
        )
