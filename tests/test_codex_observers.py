from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_codex_capacity import FakeClient, FakeServer

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "plugins/agent/.agents/agent/runtime")
)

from codex import (
    capacity,
    client,
    frontend_input,
    sessions,
    shell_commands,
)


def append_event(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value) + "\n")


@pytest.fixture
def recording(tmp_path: Path) -> Any:
    path = tmp_path / "events.jsonl"
    append_event(path, {"dir": "meta", "kind": "session_start"})
    append_event(
        path,
        {
            "dir": "from_tui",
            "kind": "op",
            "payload": {
                "UserTurn": {"client_user_message_id": "manual", "cwd": str(tmp_path)}
            },
        },
    )
    value = frontend_input.FrontendInput(path, {"ThreadChanged"})
    value.poll()
    return value


@pytest.mark.parametrize(
    "control",
    [
        {"dir": "from_tui", "kind": "op", "payload": "Interrupt"},
        {"dir": "from_tui", "kind": "op", "payload": {"Interrupt": {}}},
        {"dir": "from_tui", "kind": "op", "payload": {"UnknownOperation": {}}},
        {"dir": "to_tui", "kind": "new_session"},
        {"dir": "to_tui", "kind": "clear_ui"},
        {"dir": "to_tui", "variant": "ThreadChanged"},
        {"dir": "meta", "kind": "session_end"},
    ],
)
def test_frontend_controls_invalidate_both_observers(
    recording: Any, control: dict[str, Any]
) -> None:
    epoch = recording.epoch
    append_event(recording.stream.path, control)
    recording.poll()
    assert recording.latest is None
    assert recording.pending is None
    assert recording.epoch > epoch


def test_automatic_input_attribution_is_shared_but_scoped_to_thread_and_control(
    recording: Any,
) -> None:
    ticket = {
        "token": "token",
        "pid": 1,
        "backend": {"pid": 2},
        "profile": "profile",
        "created_at": 1,
    }
    recording.stream.path.with_name("client.json").write_text(
        json.dumps(ticket), encoding="utf-8"
    )
    frontend_input.bind_input(recording, ticket, "thread", "automatic")
    assert frontend_input.attributed_input(recording) == "automatic"
    assert frontend_input.observer_input(recording, "thread") == "automatic"
    assert frontend_input.observer_input(recording, "different-thread") == "manual"
    append_event(
        recording.stream.path,
        {
            "dir": "from_tui",
            "kind": "op",
            "ts": "2026-11-04T12:00:00Z",
            "payload": {"RunUserShellCommand": {"command": "echo done"}},
        },
    )
    recording.poll()
    assert frontend_input.attributed_input(recording) == "manual"
    assert frontend_input.observer_input(recording, "thread") == "automatic"
    assert frontend_input.shell_pending(recording, "thread")
    frontend_input.bind_input(recording, ticket, "thread", "shell-automatic")
    assert not frontend_input.shell_pending(recording, "thread")
    ticket["pid"] = 3
    recording.stream.path.with_name("client.json").write_text(
        json.dumps(ticket), encoding="utf-8"
    )
    assert frontend_input.observer_input(recording, "thread") == "manual"


def test_frontend_prepare_attach_sets_real_pid_before_starting_both_observers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket: dict[str, Any] = {"token": "token", "backend": {"pid": 2}}
    observed: list[tuple[str, int]] = []

    def prepare(*args: Any, **kwargs: Any) -> Any:
        return [], {}, ticket

    def attach(value: dict[str, Any], pid: int) -> None:
        assert "pid" not in value
        assert not any(key.startswith(("_shell_", "_capacity_")) for key in value)

    def start_shell(value: dict[str, Any], _client: Any) -> None:
        observed.append(("shell", value["pid"]))

    def start_capacity(value: dict[str, Any]) -> None:
        observed.append(("capacity", value["pid"]))

    monkeypatch.setattr(client, "prepare", prepare)
    monkeypatch.setattr(client, "attach", attach)
    monkeypatch.setattr(client, "observer_binding", lambda value: value)
    monkeypatch.setattr(shell_commands, "start", start_shell)
    monkeypatch.setattr(capacity, "start", start_capacity)
    _, _, prepared = sessions.prepare_frontend([], tmp_path, {}, executable="codex")
    sessions.attach_frontend(prepared, 123)
    assert observed == [("shell", 123), ("capacity", 123)]
    assert ticket["_shell_executable"] == ticket["_capacity_executable"] == "codex"


def test_unsupported_backend_never_starts_observers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket: dict[str, Any] = {"token": "a" * 64, "backend": {"unknown": True}}
    started: list[str] = []

    monkeypatch.setattr(client, "attach", lambda _ticket, _pid: None)
    monkeypatch.setattr(
        client,
        "observer_binding",
        lambda _ticket: (_ for _ in ()).throw(RuntimeError("unsupported")),
    )
    monkeypatch.setattr(shell_commands, "start", lambda *_args: started.append("shell"))
    monkeypatch.setattr(capacity, "start", lambda *_args: started.append("capacity"))

    sessions.attach_frontend(ticket, 123)

    assert not started


def test_failed_capacity_shutdown_preserves_monitor_and_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = capacity.CapacityMonitor({"created_at": 0})
    ticket = {"_capacity_monitor": observer}
    stopped: list[str] = []

    def refuse() -> None:
        raise RuntimeError("observer still alive")

    def stop_shell(_ticket: Any) -> None:
        stopped.append("shell")

    def release(_ticket: Any) -> None:
        raise AssertionError("binding must survive a failed observer stop")

    monkeypatch.setattr(observer, "stop", refuse)
    monkeypatch.setattr(shell_commands, "stop", stop_shell)
    monkeypatch.setattr(client, "release", release)
    with pytest.raises(RuntimeError, match="observer still alive"):
        sessions.release_frontend(ticket)
    assert ticket["_capacity_monitor"] is observer
    assert stopped == ["shell"]


def test_capacity_recovers_verified_shell_generated_input(
    recording: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket: dict[str, Any] = {
        "created_at": 0,
        "recording_path": str(recording.stream.path),
        "profile": "/profile",
        "backend": {"pid": 10, "process_start_time": "123"},
        "pid": 20,
        "launch_cwd": str(tmp_path.resolve()),
        "token": "binding-token",
        "_capacity_executable": "/profile/codex",
        "_capacity_record_identity": recording.stream.identity,
    }
    native_client = FakeClient(recording.stream.path)
    native_client.binding.write_text(json.dumps(ticket), encoding="utf-8")
    frontend_input.bind_input(recording, ticket, "thread-1", "shell-automatic")
    observer = capacity.CapacityMonitor(ticket, clock=lambda: 1)
    server = FakeServer(tmp_path)
    server.client_id = "shell-automatic"
    monkeypatch.setattr(capacity, "DELAYS", (0,))
    monkeypatch.setattr(capacity, "state_root", lambda: tmp_path / "state")
    candidate = {"thread_id": "thread-1", "turn_id": "shell-turn", "cwd": str(tmp_path)}
    observer._recover(server, native_client, candidate, "manual", recording.epoch)
    assert len(server.sent) == 1
    assert "shell-automatic" in observer.auto_ids
    assert (
        frontend_input.attributed_input(recording)
        == server.sent[0][1]["clientUserMessageId"]
    )
