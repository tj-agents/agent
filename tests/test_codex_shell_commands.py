from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".codex/runtime"))
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))

from codex import client, frontend_input, sessions, shell_commands  # noqa: E402

COMMAND = "echo tj-shell-continuation-proof"


def append(path: Path, *rows: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def frontend_op(
    payload: dict[str, Any] | str, timestamp: str = "2026-11-03T22:10:15.900Z"
) -> dict[str, Any]:
    return {"ts": timestamp, "dir": "from_tui", "kind": "op", "payload": payload}


def native_input(identity: str = "manual-input") -> dict[str, Any]:
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "thread_id": "thread-1",
            "turn_id": "input-turn",
            "item": {"type": "UserMessage", "client_id": identity},
        },
    }


def fixture_rows() -> list[dict[str, Any]]:
    path = ROOT / "tests/fixtures/codex-user-shell-0.163.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class FakeServer:
    def __init__(self, profile: Path, cwd: Path, rollout: Path) -> None:
        self.metadata: dict[str, Any] = {
            "id": "thread-1",
            "cwd": str(cwd.resolve()),
            "source": "vscode",
            "parentThreadId": None,
            "canAcceptDirectInput": True,
            "status": {"type": "idle"},
            "path": str(rollout),
        }
        self.sent: list[dict[str, Any]] = []
        self.before_read: Callable[[], None] | None = None
        self.after_send: Callable[[], None] | None = None
        self.profile = profile
        self.loaded = ["thread-1"]

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "thread/loaded/list":
            return {"data": self.loaded, "nextCursor": None}
        if method == "thread/read":
            if self.before_read is not None:
                action, self.before_read = self.before_read, None
                action()
            assert params == {"threadId": "thread-1", "includeTurns": False}
            return {"thread": self.metadata}
        if method == "turn/start":
            self.sent.append(params)
            if self.after_send is not None:
                self.after_send()
            return {}
        raise AssertionError(f"unsupported native method {method}")


@pytest.fixture
def monitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[shell_commands.ShellMonitor, FakeServer]:
    profile = tmp_path / "profile"
    sessions = profile / "sessions"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout.jsonl"
    rows = fixture_rows()
    append(rollout, rows[0], native_input(), *rows[1:])
    recording = tmp_path / "frontend.jsonl"
    append(
        recording,
        {"dir": "meta", "kind": "session_start"},
        frontend_op(
            {
                "UserTurn": {
                    "client_user_message_id": "manual-input",
                    "cwd": str(tmp_path),
                }
            }
        ),
        frontend_op({"RunUserShellCommand": {"command": COMMAND}}),
    )
    ticket: dict[str, Any] = {
        "schema_version": 1,
        "token": "a" * 64,
        "host": "codex",
        "pid": 20,
        "backend": {"pid": 10, "process_start_time": "123"},
        "profile": str(profile),
        "launch_cwd": str(tmp_path),
        "recording_path": str(recording),
        "created_at": 1.0,
        "_shell_executable": str(tmp_path / "codex"),
    }
    binding = tmp_path / "client.json"
    binding.write_text(json.dumps(ticket), encoding="utf-8")
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(tmp_path / "state"))
    monkeypatch.setattr(
        client, "daemon_identity", lambda *_args, **_kwargs: ticket["backend"]
    )
    monkeypatch.setattr(client, "binding_path", lambda _token: binding)
    monkeypatch.setattr(
        client,
        "process_context",
        lambda _pid: {
            "profile": str(profile),
            client.RECORD_PATH_ENV: str(recording),
            client.RECORD_ENV: "1",
        },
    )
    monkeypatch.setattr(
        client, "process_arguments", lambda _pid: [ticket["_shell_executable"]]
    )
    instance = shell_commands.ShellMonitor(ticket, client)
    return instance, FakeServer(profile, tmp_path, rollout)


def test_native_fixture_resumes_once_without_reexecuting_shell(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    instance.step(server)
    instance.step(server)
    assert len(server.sent) == 1
    request = server.sent[0]
    assert request["threadId"] == "thread-1"
    assert request["clientUserMessageId"] in instance.own_ids
    assert request["input"] == [
        {"type": "text", "text": shell_commands.CONTINUATION, "text_elements": []}
    ]
    assert set(request) == {"threadId", "clientUserMessageId", "input"}


def test_shell_guard_uses_the_durable_observer_binding(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, server = monitor
    monkeypatch.setattr(
        client, "observer_binding", lambda _ticket: {"backend": {"kind": "handoff"}}
    )
    monkeypatch.setattr(
        client,
        "daemon_identity",
        lambda *_args, **_kwargs: pytest.fail("shell selected a shared daemon"),
    )
    instance.step(server)
    assert len(server.sent) == 1


def test_shell_after_capacity_continuation_keeps_verified_input_attribution(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    path = Path(server.metadata["path"])
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows[:2])
        + json.dumps(native_input("capacity-automatic"))
        + "\n"
        + "".join(json.dumps(row) + "\n" for row in rows[2:]),
        encoding="utf-8",
    )
    recording = instance.frontend.stream.path
    controls = recording.read_text(encoding="utf-8").splitlines()
    recording.write_text("\n".join(controls[:2]) + "\n", encoding="utf-8")
    prior = frontend_input.FrontendInput(recording, set())
    prior.poll()
    frontend_input.bind_input(prior, instance.ticket, "thread-1", "capacity-automatic")
    append(recording, json.loads(controls[2]))
    instance.step(server)
    assert len(server.sent) == 1
    assert "capacity-automatic" in instance.own_ids


def test_failed_command_still_resumes_from_its_recorded_result(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    path = Path(server.metadata["path"])
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[3]["payload"]["item"].update(status="failed", exit_code=7)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    instance.step(server)
    assert len(server.sent) == 1


@pytest.mark.parametrize(
    "change",
    [
        "Interrupt",
        "Compact",
        {"UserTurn": {"client_user_message_id": "new-manual-input"}},
    ],
)
def test_new_manual_input_or_interrupt_prevents_stale_continuation(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
    change: dict[str, Any] | str,
) -> None:
    instance, server = monitor
    append(instance.frontend.stream.path, frontend_op(change))
    instance.step(server)
    assert not server.sent


@pytest.mark.parametrize(
    "variant", ["ResetTranscriptForThreadSwitch", "OpenResumePicker"]
)
def test_thread_navigation_prevents_stale_continuation(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
    variant: str,
) -> None:
    instance, server = monitor
    append(
        instance.frontend.stream.path,
        {"dir": "to_tui", "kind": "app_event", "variant": variant},
    )
    instance.step(server)
    assert not server.sent


def test_input_arriving_during_native_read_prevents_submission(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    server.before_read = lambda: append(
        instance.frontend.stream.path, frontend_op("Interrupt")
    )
    instance.step(server)
    assert not server.sent


def test_input_from_another_frontend_prevents_submission(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    append(Path(server.metadata["path"]), native_input("other-frontend-input"))
    instance.step(server)
    assert not server.sent


def test_auxiliary_shell_in_an_agent_turn_needs_no_second_turn(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    path = Path(server.metadata["path"])
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows.insert(3, native_input())
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    instance.step(server)
    assert not server.sent


@pytest.mark.parametrize("removed", [3, 4, 5])
def test_execution_result_and_turn_completion_are_all_required(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
    removed: int,
) -> None:
    instance, server = monitor
    path = Path(server.metadata["path"])
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows.pop(removed)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    instance.step(server)
    assert not server.sent


@pytest.mark.parametrize("field,value", [("source", "agent"), ("status", "declined")])
def test_agent_issued_or_declined_commands_do_not_resume(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
    field: str,
    value: str,
) -> None:
    instance, server = monitor
    path = Path(server.metadata["path"])
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[3]["payload"]["item"][field] = value
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    instance.step(server)
    assert not server.sent


def test_identical_old_command_cannot_satisfy_a_new_request(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    append(
        instance.frontend.stream.path,
        frontend_op(
            {"RunUserShellCommand": {"command": COMMAND}}, "2026-11-03T22:10:19.000Z"
        ),
    )
    instance.step(server)
    assert not server.sent


def test_partial_native_record_defers_until_the_result_is_complete(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    path = Path(server.metadata["path"])
    data = path.read_bytes()
    path.write_bytes(data[:-2])
    instance.step(server)
    assert not server.sent
    with path.open("ab") as stream:
        stream.write(data[-2:])
    instance.step(server)
    assert len(server.sent) == 1


def test_receipt_survives_observer_restart_and_prevents_duplicates(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    instance.step(server)
    replacement = shell_commands.ShellMonitor(instance.ticket, client)
    replacement.step(server)
    assert len(server.sent) == 1


def test_uncertain_native_acceptance_is_not_replayed(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor

    def disconnect() -> None:
        raise RuntimeError("connection lost after native acceptance")

    server.after_send = disconnect
    instance.step(server)
    instance.step(server)
    assert len(server.sent) == 1


def test_changed_frontend_process_refuses_continuation(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, server = monitor
    monkeypatch.setattr(
        client, "process_arguments", lambda _pid: ["another-native-client"]
    )
    instance.step(server)
    assert not server.sent


def test_changed_backend_identity_refuses_continuation(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, server = monitor
    monkeypatch.setattr(
        client, "daemon_identity", lambda *_args, **_kwargs: {"pid": 99}
    )
    instance.step(server)
    assert not server.sent


def test_non_idle_thread_is_never_interrupted_by_continuation(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    server.metadata["status"] = {"type": "active"}
    instance.step(server)
    assert not server.sent


def test_rollout_outside_verified_profile_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="outside its verified profile"):
        shell_commands.Rollout(
            outside, tmp_path / "profile", "thread-1", client.comparable_path
        )


def test_replaced_native_rollout_is_rejected(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    server.metadata["status"] = {"type": "active"}
    instance.step(server)
    server.metadata["status"] = {"type": "idle"}
    path = Path(server.metadata["path"])
    replacement = path.with_name("replacement.jsonl")
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    with pytest.raises(RuntimeError, match="replaced or truncated"):
        instance.step(server)


def test_shutdown_prevents_a_late_callback(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    instance.stop()
    instance.step(server)
    assert not server.sent


def test_subsequent_command_after_automatic_response_continues_again(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    instance.step(server)
    first = server.sent[0]["clientUserMessageId"]
    path = Path(server.metadata["path"])
    append(
        path,
        native_input(first),
        {"type": "response_item", "payload": {"role": "assistant"}},
    )
    rows = fixture_rows()[1:]
    for row in rows:
        payload = row["payload"]
        payload["turn_id"] = "second-shell-turn"
        if payload.get("type") == "item_completed":
            payload["item"]["id"] = "second-shell-item"
            payload["started_at_ms"] += 5000
        if "internal_chat_message_metadata_passthrough" in payload:
            payload["internal_chat_message_metadata_passthrough"]["turn_id"] = (
                "second-shell-turn"
            )
    append(
        instance.frontend.stream.path,
        frontend_op(
            {"RunUserShellCommand": {"command": COMMAND}}, "2026-11-03T22:10:19.000Z"
        ),
    )
    append(path, *rows)
    instance.step(server)
    instance.step(server)
    assert len(server.sent) == 2
    assert server.sent[1]["clientUserMessageId"] != first


def test_frontend_without_a_prior_attributable_input_is_not_guessed(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    path = instance.frontend.stream.path
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows.pop(1)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    instance.step(server)
    assert not server.sent


def test_malformed_turn_identity_is_rejected(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    path = Path(server.metadata["path"])
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[2]["payload"]["turn_id"] = None
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(RuntimeError, match="valid identity"):
        instance.step(server)
    assert not server.sent


def test_frontend_release_waits_for_observer_before_deleting_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[str] = []
    monkeypatch.setattr(
        sessions.shell_commands, "stop", lambda _ticket: actions.append("stop")
    )
    monkeypatch.setattr(
        sessions.client, "release", lambda _ticket: actions.append("release")
    )
    sessions.release_frontend({})
    assert actions == ["stop", "release"]


def test_failed_observer_shutdown_preserves_frontend_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex import sessions

    actions: list[str] = []

    def fail(_ticket: dict[str, Any]) -> None:
        raise RuntimeError("observer did not stop")

    monkeypatch.setattr(sessions.shell_commands, "stop", fail)
    monkeypatch.setattr(
        sessions.client, "release", lambda _ticket: actions.append("release")
    )
    with pytest.raises(RuntimeError, match="did not stop"):
        sessions.release_frontend({})
    assert not actions


def test_automatic_response_remains_attributable_to_its_frontend(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    instance.step(server)
    identity = server.sent[0]["clientUserMessageId"]
    assert client.latest_input(instance.frontend.stream.path) == identity
    append(
        instance.frontend.stream.path,
        frontend_op({"ExecApproval": {"id": "approval", "decision": "Approved"}}),
    )
    assert client.latest_input(instance.frontend.stream.path) == identity


@pytest.mark.parametrize(
    "change",
    [
        "Interrupt",
        {"UserTurn": {"client_user_message_id": "new-input"}},
        {"RunUserShellCommand": {"command": "echo next"}},
    ],
)
def test_new_frontend_control_cancels_automatic_input_binding(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
    change: dict[str, Any] | str,
) -> None:
    instance, server = monitor
    instance.step(server)
    identity = server.sent[0]["clientUserMessageId"]
    append(instance.frontend.stream.path, frontend_op(change))
    assert client.latest_input(instance.frontend.stream.path) != identity


def test_changed_process_binding_cannot_claim_automatic_input(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    instance.step(server)
    path = instance.frontend.stream.path.with_name("client.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    value["pid"] += 1
    path.write_text(json.dumps(value), encoding="utf-8")
    assert client.latest_input(instance.frontend.stream.path) == "manual-input"


def test_native_notification_queue_is_drained_without_answering_requests(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    import queue
    from types import SimpleNamespace

    instance, server = monitor
    messages: queue.Queue[Any] = queue.Queue()
    messages.put({"method": "item/started", "params": {}})
    messages.put(
        {
            "id": "approval",
            "method": "item/commandExecution/requestApproval",
            "params": {},
        }
    )
    instance.discard_notifications(SimpleNamespace(messages=messages))
    assert messages.empty()
    assert not server.sent


@pytest.mark.parametrize("identity", [None, "", 7])
def test_unattributed_native_input_invalidates_the_prior_anchor(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer], identity: Any
) -> None:
    instance, server = monitor
    path = Path(server.metadata["path"])
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    other = native_input()
    other["payload"]["item"]["client_id"] = identity
    rows.insert(2, other)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    instance.step(server)
    assert not server.sent


def test_current_recorded_directory_can_differ_from_launcher_directory(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer], tmp_path: Path
) -> None:
    instance, server = monitor
    current = tmp_path / "selected-project"
    current.mkdir()
    server.metadata["cwd"] = str(current)
    path = instance.frontend.stream.path
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[1]["payload"]["UserTurn"]["cwd"] = str(current)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    instance.step(server)
    assert len(server.sent) == 1


@pytest.mark.parametrize("directory", [None, "relative-directory"])
def test_unattributed_directory_never_starts_a_continuation(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer], directory: str | None
) -> None:
    instance, server = monitor
    path = instance.frontend.stream.path
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[1]["payload"]["UserTurn"]["cwd"] = directory
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    instance.step(server)
    assert not server.sent


@pytest.mark.skipif(sys.platform != "win32", reason="Windows extended-length paths")
def test_equivalent_windows_rollout_spelling_is_accepted(
    monitor: tuple[shell_commands.ShellMonitor, FakeServer],
) -> None:
    instance, server = monitor
    server.metadata["path"] = "\\\\?\\" + server.metadata["path"]
    instance.step(server)
    assert len(server.sent) == 1
