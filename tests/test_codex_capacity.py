"""Codex capacity recovery fails closed unless exact input and history agree."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

RUNTIME = Path(__file__).resolve().parents[1] / ".codex/runtime"
BUNDLED_RUNTIME = (
    Path(__file__).resolve().parents[1] / "plugins/agent/.agents/agent/runtime"
)
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(BUNDLED_RUNTIME))

from codex import capacity, frontend_input  # noqa: E402
from codex import client as bundled_client  # noqa: E402


class FakeServer:
    def __init__(self, cwd: Path) -> None:
        self.cwd = str(cwd.resolve())
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.client_id = "manual-input"
        self.history_visible = True
        self.loaded = True
        self.can_accept_direct_input = True
        self.source = "cli"
        self.thread_status = "systemError"
        self.error_code = "serverOverloaded"

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "thread/turns/list":
            return {
                "data": [
                    {
                        "id": "failed-turn",
                        "status": "failed",
                        "error": {"codexErrorInfo": self.error_code},
                    }
                ]
            }
        if method == "thread/items/list":
            return {
                "data": (
                    [{"item": {"type": "userMessage", "clientId": self.client_id}}]
                    if self.history_visible
                    else []
                ),
                "nextCursor": None,
            }
        if method == "thread/loaded/list":
            return {
                "data": ["thread-1"] if self.loaded else [],
                "nextCursor": None,
            }
        if method == "thread/read":
            return {
                "thread": {
                    "id": "thread-1",
                    "cwd": self.cwd,
                    "canAcceptDirectInput": self.can_accept_direct_input,
                    "source": self.source,
                    "status": {"type": self.thread_status},
                }
            }
        if method == "turn/start":
            self.sent.append((method, params))
            return {}
        raise AssertionError(f"unexpected app-server request: {method}")


class DisconnectAfterSendServer(FakeServer):
    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "turn/start":
            self.sent.append((method, params))
            self.client_id = params["clientUserMessageId"]
            raise RuntimeError("proxy disconnected after submission")
        return super().request(method, params)


class FakeClient:
    RECORD_PATH_ENV = "CODEX_TUI_SESSION_LOG_PATH"
    RECORD_ENV = "CODEX_TUI_RECORD_SESSION"

    def __init__(self, recording: Path):
        self.recording = recording
        self.binding = recording.with_name("client.json")

    def daemon_identity(
        self, environment: dict[str, str], *, expected_pid: int
    ) -> dict[str, Any]:
        assert environment == {"CODEX_HOME": "/profile"}
        assert expected_pid == 10
        return {"pid": 10, "process_start_time": "123"}

    def process_context(self, pid: int) -> dict[str, str]:
        assert pid == 20
        return {
            "profile": "/profile",
            self.RECORD_PATH_ENV: str(self.recording),
            self.RECORD_ENV: "1",
        }

    def binding_path(self, token: str) -> Path:
        assert token == "binding-token"
        return self.binding

    def observer_binding(self, ticket: dict[str, Any]) -> dict[str, Any]:
        record = cast(
            dict[str, Any], json.loads(self.binding.read_text(encoding="utf-8"))
        )
        for key in (
            "token",
            "pid",
            "backend",
            "profile",
            "recording_path",
            "created_at",
        ):
            if record.get(key) != ticket.get(key):
                raise RuntimeError("binding changed")
        return record

    @staticmethod
    def process_arguments(pid: int) -> list[str]:
        assert pid == 20
        return ["/profile/codex", "resume", "thread-1"]

    @staticmethod
    def is_session(arguments: list[str]) -> bool:
        assert arguments == ["resume", "thread-1"]
        return True

    @staticmethod
    def comparable_path(path: Path) -> Path:
        return path


def test_capacity_backend_guard_uses_the_durable_observer_binding(
    tmp_path: Path,
) -> None:
    monitor = capacity.CapacityMonitor(
        {"recording_path": str(tmp_path / "events.jsonl"), "created_at": 0}
    )

    class OwnedClient:
        def observer_binding(self, _ticket: dict[str, Any]) -> dict[str, Any]:
            return {"backend": {"kind": "handoff"}}

        def daemon_identity(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("capacity selected a shared daemon")

    assert monitor._backend_matches(OwnedClient())


class FakeProxy:
    def __init__(self) -> None:
        self.alive = True
        self.terminated = False

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        assert timeout == 3
        return 0


class FakeMonitorThread:
    def __init__(self) -> None:
        self.joined = False

    def is_alive(self) -> bool:
        return not self.joined

    def join(self, timeout: float | None = None) -> None:
        assert timeout == 8
        self.joined = True


def test_capacity_failure_requires_terminal_exact_capacity_error() -> None:
    assert capacity.capacity_failure(
        {"status": "failed", "error": {"codexErrorInfo": "serverOverloaded"}}
    )
    assert capacity.capacity_failure(
        {"status": "failed", "error": {"message": capacity.CAPACITY_ERROR}}
    )
    assert not capacity.capacity_failure(
        {"status": "inProgress", "error": {"codexErrorInfo": "serverOverloaded"}}
    )
    assert not capacity.capacity_failure(
        {"status": "failed", "error": {"codexErrorInfo": "unauthorized"}}
    )
    assert not capacity.capacity_failure(
        {"status": "failed", "error": {"message": "model capacity is limited"}}
    )


def test_frontend_preflight_uses_only_bundled_client_binding_apis() -> None:
    assert callable(bundled_client.process_context)
    assert callable(bundled_client.process_arguments)
    assert callable(bundled_client.binding_path)
    assert not hasattr(bundled_client, "process_info")


def test_candidate_hook_subprocess_records_no_prompt_text(tmp_path: Path) -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "plugins/agent/.agents/agent/runtime/codex/capacity.py"
    )
    assert script.is_file()
    environment = dict(
        os.environ,
        AGENT_STATE_DIRECTORY=str(tmp_path / "state"),
        CODEX_HOME=str(tmp_path / "profile"),
    )
    result = subprocess.run(
        [sys.executable, "-B", str(script), "candidate"],
        input=json.dumps(
            {
                "session_id": "thread-1",
                "turn_id": "turn-1",
                "cwd": str(tmp_path),
                "prompt": "secret prompt must not be persisted",
            }
        ),
        text=True,
        capture_output=True,
        env=environment,
        check=True,
    )
    assert result.returncode == 0
    files = list((tmp_path / "state" / "capacity-inputs").rglob("*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert saved["thread_id"] == "thread-1"
    assert saved["turn_id"] == "turn-1"
    assert "prompt" not in saved


def test_recovery_reserves_one_exact_thread_continuation_without_model_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = tmp_path / "events.jsonl"
    recording.write_text(
        json.dumps({"dir": "meta", "kind": "session_start"})
        + "\n"
        + json.dumps(
            {
                "dir": "from_tui",
                "kind": "op",
                "payload": {
                    "UserTurn": {"client_user_message_id": "manual-input"},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(capacity, "DELAYS", (0,))
    monkeypatch.setattr(capacity, "state_root", lambda: tmp_path / "state")
    monitor = capacity.CapacityMonitor(
        {
            "created_at": 0,
            "recording_path": str(recording),
            "_capacity_record_identity": None,
            "profile": "/profile",
            "backend": {"pid": 10, "process_start_time": "123"},
            "pid": 20,
            "launch_cwd": str(tmp_path.resolve()),
            "token": "binding-token",
            "_capacity_executable": "/profile/codex",
        },
        clock=lambda: 1.0,
    )
    identity = frontend_input.FrontendInput(recording, set()).snapshot()[2]
    monitor.ticket["_capacity_record_identity"] = identity
    server = FakeServer(tmp_path)
    client = FakeClient(recording)
    client.binding.write_text(
        json.dumps(
            {
                "token": "binding-token",
                "pid": 20,
                "backend": {"pid": 10, "process_start_time": "123"},
                "profile": "/profile",
                "recording_path": str(recording),
                "created_at": 0,
            }
        ),
        encoding="utf-8",
    )
    candidate = {
        "thread_id": "thread-1",
        "turn_id": "manual-turn",
        "cwd": str(tmp_path.resolve()),
    }
    monitor._recover(server, client, candidate, "manual-input", 2)
    second_observer = capacity.CapacityMonitor(monitor.ticket, clock=lambda: 1.0)
    second_observer._recover(server, client, candidate, "manual-input", 2)
    assert len(server.sent) == 1
    method, params = server.sent[0]
    assert method == "turn/start"
    assert params["threadId"] == "thread-1"
    assert "model" not in params and "effort" not in params and "cwd" not in params
    assert params["input"][0]["text_elements"] == []
    assert "authorized" in params["input"][0]["text"]
    receipts = list((tmp_path / "state" / "capacity-recovery").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["client_user_message_id"] == params["clientUserMessageId"]


def test_disconnect_after_submission_reconciles_the_same_receipt_without_resend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = tmp_path / "events.jsonl"
    recording.write_text(
        json.dumps({"dir": "meta", "kind": "session_start"})
        + "\n"
        + json.dumps(
            {
                "dir": "from_tui",
                "kind": "op",
                "payload": {"UserTurn": {"client_user_message_id": "manual-input"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(capacity, "DELAYS", (0,))
    monkeypatch.setattr(capacity, "state_root", lambda: tmp_path / "state")
    ticket: dict[str, Any] = {
        "created_at": 0,
        "recording_path": str(recording),
        "profile": "/profile",
        "backend": {"pid": 10, "process_start_time": "123"},
        "pid": 20,
        "launch_cwd": str(tmp_path.resolve()),
        "token": "binding-token",
        "_capacity_executable": "/profile/codex",
        "_capacity_record_identity": frontend_input.FrontendInput(
            recording, set()
        ).snapshot()[2],
    }
    client = FakeClient(recording)
    client.binding.write_text(
        json.dumps(
            {
                "token": "binding-token",
                "pid": 20,
                "backend": ticket["backend"],
                "profile": "/profile",
                "recording_path": str(recording),
                "created_at": 0,
            }
        ),
        encoding="utf-8",
    )
    server = DisconnectAfterSendServer(tmp_path)
    candidate = {
        "thread_id": "thread-1",
        "turn_id": "manual-turn",
        "cwd": str(tmp_path.resolve()),
    }
    for _ in range(2):
        observer = capacity.CapacityMonitor(ticket, clock=lambda: 1.0)
        observer._recover(server, client, candidate, "manual-input", 2)
    assert len(server.sent) == 1


def test_repeated_native_system_errors_retry_five_times_then_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = tmp_path / "events.jsonl"
    recording.write_text(
        json.dumps({"dir": "meta", "kind": "session_start"})
        + "\n"
        + json.dumps(
            {
                "dir": "from_tui",
                "kind": "op",
                "payload": {"UserTurn": {"client_user_message_id": "manual-input"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ticket = {
        "created_at": 0,
        "recording_path": str(recording),
        "profile": "/profile",
        "backend": {"pid": 10, "process_start_time": "123"},
        "pid": 20,
        "launch_cwd": str(tmp_path.resolve()),
        "token": "binding-token",
        "_capacity_executable": "/profile/codex",
        "_capacity_record_identity": frontend_input.FrontendInput(
            recording, set()
        ).snapshot()[2],
    }
    client = FakeClient(recording)
    client.binding.write_text(
        json.dumps(
            {
                key: ticket[key]
                for key in (
                    "token",
                    "pid",
                    "backend",
                    "profile",
                    "recording_path",
                    "created_at",
                )
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(capacity, "DELAYS", (0, 0, 0, 0, 0))
    monkeypatch.setattr(capacity, "state_root", lambda: tmp_path / "state")

    class RepeatedCapacity(FakeServer):
        def __init__(self) -> None:
            super().__init__(tmp_path)
            self.sequence = 0
            self.messages = {"retry-0": "manual-input", "manual-turn": "manual-input"}
            self.completed = False

        def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "thread/turns/list":
                return {
                    "data": [
                        {
                            "id": f"retry-{self.sequence}",
                            "status": "completed" if self.completed else "failed",
                            "error": {"codexErrorInfo": "serverOverloaded"},
                        }
                    ]
                }
            if method == "thread/items/list":
                return {
                    "data": [
                        {
                            "item": {
                                "type": "userMessage",
                                "clientId": self.messages[params["turnId"]],
                            }
                        }
                    ],
                    "nextCursor": None,
                }
            result = super().request(method, params)
            if method == "turn/start":
                self.sequence += 1
                self.messages[f"retry-{self.sequence}"] = params["clientUserMessageId"]
            return result

    server = RepeatedCapacity()
    monitor = capacity.CapacityMonitor(ticket, clock=lambda: 1)
    candidate = {
        "thread_id": "thread-1",
        "turn_id": "manual-turn",
        "cwd": str(tmp_path.resolve()),
    }
    for _ in range(7):
        monitor._recover(server, client, candidate, "manual-input", 2)
    assert len(server.sent) == 5
    assert len({params["clientUserMessageId"] for _, params in server.sent}) == 5
    server.completed = True
    monitor._recover(server, client, candidate, "manual-input", 2)
    assert len(server.sent) == 5


def test_capacity_attempt_budget_stops_at_five(tmp_path: Path) -> None:
    recording = tmp_path / "events.jsonl"
    recording.write_text("", encoding="utf-8")
    monitor = capacity.CapacityMonitor(
        {
            "created_at": 0,
            "recording_path": str(recording),
            "profile": "/profile",
            "backend": {"pid": 10, "process_start_time": "123"},
            "pid": 20,
            "launch_cwd": str(tmp_path.resolve()),
        }
    )
    monitor.attempts["manual-input"] = 5
    server = FakeServer(tmp_path)
    monitor._recover(
        server,
        FakeClient(recording),
        {"thread_id": "thread-1"},
        "manual-input",
        1,
    )
    assert server.sent == []


def test_new_manual_input_during_retry_delay_cancels_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording = tmp_path / "events.jsonl"
    manual = {
        "dir": "from_tui",
        "kind": "op",
        "payload": {"UserTurn": {"client_user_message_id": "manual-input"}},
    }
    recording.write_text(
        json.dumps({"dir": "meta", "kind": "session_start"})
        + "\n"
        + json.dumps(manual)
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(capacity, "DELAYS", (5,))
    monitor = capacity.CapacityMonitor(
        {
            "created_at": 0,
            "recording_path": str(recording),
            "profile": "/profile",
            "backend": {"pid": 10, "process_start_time": "123"},
            "pid": 20,
            "launch_cwd": str(tmp_path.resolve()),
        },
        clock=lambda: 1.0,
    )
    monitor.recording.poll()
    monitor.ticket["_capacity_record_identity"] = monitor.recording.stream.identity

    def replace_input(_timeout: float) -> bool:
        with recording.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "dir": "from_tui",
                        "kind": "op",
                        "payload": {
                            "UserTurn": {"client_user_message_id": "newer-input"}
                        },
                    }
                )
                + "\n"
            )
        return False

    monkeypatch.setattr(monitor.stop_event, "wait", replace_input)
    server = FakeServer(tmp_path)
    monitor._recover(
        server,
        FakeClient(recording),
        {"thread_id": "thread-1"},
        "manual-input",
        monitor.recording.epoch,
    )
    assert server.sent == []


def test_candidate_path_is_scoped_by_profile_and_turn(tmp_path: Path) -> None:
    left = capacity.candidate_path("/profile-a", "thread", "turn", tmp_path)
    right = capacity.candidate_path("/profile-b", "thread", "turn", tmp_path)
    assert left != right


def test_prompt_candidate_waits_for_history_inserted_after_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capacity, "state_root", lambda: tmp_path / "state")
    assert capacity.write_candidate(
        {
            "session_id": "thread-1",
            "turn_id": "manual-turn",
            "cwd": str(tmp_path),
            "codex_home": str(Path("/profile").resolve()),
            "prompt": "must not be needed for attribution",
        }
    )
    monitor = capacity.CapacityMonitor(
        {
            "created_at": 0,
            "profile": str(Path("/profile").resolve()),
            "launch_cwd": str(tmp_path),
        }
    )
    (candidate,) = monitor._candidates()
    server = FakeServer(tmp_path)
    server.history_visible = False
    assert not monitor._candidate_matches(server, candidate, "manual-input")
    server.history_visible = True
    assert monitor._candidate_matches(server, candidate, "manual-input")


@pytest.mark.parametrize(
    ("loaded", "can_accept", "source", "candidate_cwd"),
    [
        (False, True, "cli", None),
        (True, False, "cli", None),
        (True, True, "subAgent", None),
        (True, True, "cli", "C:/different-checkout"),
    ],
)
def test_candidate_binding_refuses_unloaded_private_or_mismatched_thread(
    tmp_path: Path,
    loaded: bool,
    can_accept: bool,
    source: str,
    candidate_cwd: str | None,
) -> None:
    monitor = capacity.CapacityMonitor(
        {"created_at": 0, "profile": "/profile", "launch_cwd": str(tmp_path)}
    )
    server = FakeServer(tmp_path)
    server.loaded = loaded
    server.can_accept_direct_input = can_accept
    server.source = source
    candidate = {
        "thread_id": "thread-1",
        "turn_id": "manual-turn",
        "cwd": candidate_cwd or str(tmp_path.resolve()),
    }
    assert not monitor._candidate_matches(server, candidate, "manual-input")


def test_bound_thread_survives_candidate_inbox_churn_until_input_epoch_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = capacity.CapacityMonitor(
        {"created_at": 0, "profile": "/profile", "launch_cwd": str(tmp_path)}
    )
    bound = {"thread_id": "thread-1", "turn_id": "manual-turn"}
    monitor.binding_key = ("manual-input", 7)
    monitor.bound_candidate = bound

    def inbox_churned() -> list[dict[str, Any]]:
        raise AssertionError("a bound exact input must not rescan the candidate inbox")

    monkeypatch.setattr(monitor, "_candidates", inbox_churned)
    assert monitor._bound_candidate_for(None, "manual-input", 7) is bound
    monkeypatch.setattr(monitor, "_candidates", list)
    assert monitor._bound_candidate_for(None, "manual-input", 8) is None
    assert monitor.bound_candidate is None


@pytest.mark.parametrize("runtime_state", ["idle", "systemError"])
def test_final_preflight_requires_inactive_and_same_failed_capacity_turn(
    tmp_path: Path,
    runtime_state: str,
) -> None:
    monitor = capacity.CapacityMonitor(
        {"created_at": 0, "profile": "/profile", "launch_cwd": str(tmp_path)}
    )
    server = FakeServer(tmp_path)
    server.thread_status = runtime_state
    candidate = {
        "thread_id": "thread-1",
        "turn_id": "manual-turn",
        "cwd": str(tmp_path.resolve()),
    }
    assert monitor._final_preflight(server, candidate, "manual-input", "failed-turn")
    server.error_code = "unauthorized"
    assert not monitor._final_preflight(
        server, candidate, "manual-input", "failed-turn"
    )
    server.error_code = "serverOverloaded"
    server.thread_status = "active"
    assert not monitor._final_preflight(
        server, candidate, "manual-input", "failed-turn"
    )


def test_recording_reader_defers_partial_line_then_reads_only_append(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    reader = frontend_input.FrontendInput(path, set())
    row = json.dumps(
        {
            "dir": "from_tui",
            "kind": "op",
            "payload": {"UserTurn": {"client_user_message_id": "manual-input"}},
        }
    ).encode()
    path.write_bytes(row)
    assert reader.snapshot()[0] is None
    assert reader.stream.offset == 0
    with path.open("ab") as stream:
        stream.write(b"\n")
    latest, epoch, _identity = reader.snapshot()
    assert latest == "manual-input"
    assert epoch == 1
    assert reader.stream.settled()


def test_stop_terminates_only_its_proxy_then_joins_monitor() -> None:
    monitor = capacity.CapacityMonitor({"created_at": 0})
    proxy = FakeProxy()
    worker = FakeMonitorThread()
    monitor.process = proxy
    monitor.thread = worker
    monitor.stop()
    assert proxy.terminated
    assert worker.joined
