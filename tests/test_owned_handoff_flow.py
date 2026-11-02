import contextlib
import importlib.util
import io
import json
import os
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
import test_codex_followup as followup
import test_native_completion as completion
import test_startup_binding as startup


@pytest.fixture
def binding() -> Iterator[Any]:
    with ExitStack() as stack:
        fixture: Any = startup.StartupBindingTests()
        fixture.addCleanup = stack.callback
        fixture.setUp()
        yield fixture.binding


@pytest.fixture
def caller() -> Iterator[Any]:
    with ExitStack() as stack:
        fixture: Any = followup.FollowupTests()
        fixture.addCleanup = stack.callback
        fixture.setUp()
        yield fixture


def test_owned_backend_prepare_failure_never_starts_a_frontend(binding: Any) -> None:
    with (
        mock.patch.object(
            startup.SESSIONS[startup.Host.CODEX],
            "prepare_frontend",
            side_effect=RuntimeError("owned endpoint unavailable"),
        ),
        mock.patch.object(binding.subprocess, "Popen") as spawn,
        pytest.raises(binding.StartupBindingError, match="could not be prepared"),
    ):
        binding.run_session(
            startup.Host.CODEX,
            "native",
            Path.cwd(),
            [],
            require_session_start=True,
            hook_roots={},
        )
    spawn.assert_not_called()


def test_owned_frontend_attach_failure_stops_child_and_releases_lease(
    binding: Any,
) -> None:
    process = mock.Mock(pid=72)
    process.poll.return_value = None
    ticket = {"token": "fixture"}
    with (
        mock.patch.object(binding.subprocess, "Popen", return_value=process),
        mock.patch.object(
            startup.SESSIONS[startup.Host.CODEX],
            "prepare_frontend",
            return_value=([], {}, ticket),
        ),
        mock.patch.object(
            startup.SESSIONS[startup.Host.CODEX],
            "attach_frontend",
            side_effect=OSError("attach failed"),
        ),
        mock.patch.object(
            startup.SESSIONS[startup.Host.CODEX], "release_frontend"
        ) as release,
        pytest.raises(binding.StartupBindingError, match="could not be attached"),
    ):
        binding.run_session(
            startup.Host.CODEX,
            "native",
            Path.cwd(),
            [],
            require_session_start=True,
            hook_roots={},
        )
    process.terminate.assert_called_once()
    release.assert_called_once_with(ticket)


def test_ack_without_recipient_stays_one_unconfirmed_launch(binding: Any) -> None:
    process = mock.Mock(pid=72)
    process.poll.return_value = None
    process.wait.return_value = 0
    receipt = {"token": "verified", "recipient": {"thread_id": "exact"}}
    running, unconfirmed = mock.Mock(), mock.Mock()
    with (
        mock.patch.object(binding.subprocess, "Popen", return_value=process) as spawn,
        mock.patch.object(
            startup.SESSIONS[startup.Host.CODEX],
            "prepare_frontend",
            return_value=([], {}, {}),
        ),
        mock.patch.object(startup.SESSIONS[startup.Host.CODEX], "attach_frontend"),
        mock.patch.object(startup.SESSIONS[startup.Host.CODEX], "release_frontend"),
        mock.patch.object(
            startup.SESSIONS[startup.Host.CODEX],
            "handoff_recipient",
            side_effect=[None, receipt],
        ),
        mock.patch.object(binding, "_acknowledged", return_value=True),
        mock.patch.object(binding.time, "sleep"),
    ):
        assert (
            binding.run_session(
                startup.Host.CODEX,
                "native",
                Path.cwd(),
                [],
                require_session_start=True,
                hook_roots={},
                timeout=0,
                on_running=running,
                on_unconfirmed=unconfirmed,
            )
            == 0
        )
    spawn.assert_called_once()
    unconfirmed.assert_called_once_with(72)
    running.assert_called_once_with(72, client_binding=receipt)
    process.terminate.assert_not_called()


def test_old_followup_receipt_never_opens_transport(caller: Any) -> None:
    caller.receipt["version"] = 1
    caller.receipt_path.write_text(json.dumps(caller.receipt), encoding="utf-8")
    with (
        mock.patch.object(followup.FOLLOWUP, "Proxy") as proxy,
        contextlib.redirect_stdout(io.StringIO()),
    ):
        assert followup.FOLLOWUP.main(caller.arguments()) == 2
    proxy.assert_not_called()


def owned_receipt(caller: Any) -> dict[str, Any]:
    return {
        "token": "a" * 64,
        "recipient": {"thread_id": followup.THREAD, "cwd": caller.receipt["cwd"]},
        "profile": caller.receipt["codex_home"],
        "backend": {"socket_path": "fixture-socket"},
        "frontend_message_id": "frontend-anchor",
        "executable": "fixture-native",
    }


def test_new_followup_resolves_explicit_launch_receipt_once(caller: Any) -> None:
    frozen = owned_receipt(caller)
    runtime = mock.Mock()
    runtime.launch_binding.return_value = frozen
    runtime.frozen_binding.return_value = frozen
    runtime.native_contains.return_value = True
    proxy = followup.Session(caller.receipt)
    with (
        mock.patch.object(followup.FOLLOWUP, "client_runtime", return_value=runtime),
        mock.patch.object(followup.FOLLOWUP, "Proxy", return_value=proxy) as connect,
        mock.patch.object(followup.FOLLOWUP, "responsibility", return_value="owner"),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        assert (
            followup.FOLLOWUP.main(
                [
                    *caller.arguments(),
                    "--launch-receipt",
                    str(caller.root / "launch.json"),
                ]
            )
            == 0
        )
    runtime.launch_binding.assert_called_once()
    assert connect.call_args.kwargs["binding"] == frozen
    receipt = json.loads(caller.receipt_path.read_text(encoding="utf-8"))
    assert receipt["launch_binding"] == frozen


def test_followup_retry_uses_frozen_endpoint_without_resolving_launch_again(
    caller: Any,
) -> None:
    frozen = owned_receipt(caller)
    caller.receipt["launch_binding"] = frozen
    caller.receipt_path.write_text(json.dumps(caller.receipt), encoding="utf-8")
    runtime = mock.Mock()
    runtime.frozen_binding.return_value = frozen
    runtime.native_contains.return_value = True
    proxy = followup.Session(caller.receipt)
    with (
        mock.patch.object(followup.FOLLOWUP, "client_runtime", return_value=runtime),
        mock.patch.object(followup.FOLLOWUP, "Proxy", return_value=proxy) as connect,
        contextlib.redirect_stdout(io.StringIO()),
    ):
        assert (
            followup.FOLLOWUP.main(
                [
                    *caller.arguments(),
                    "--launch-receipt",
                    str(caller.root / "another-launch.json"),
                ]
            )
            == 0
        )
    runtime.launch_binding.assert_not_called()
    assert connect.call_args.kwargs["binding"] == frozen
    assert not any(method.startswith("turn/") for method, _ in proxy.calls)


def test_missing_owned_backend_preserves_receipt_without_opening_shared_proxy(
    caller: Any,
) -> None:
    caller.receipt["launch_binding"] = owned_receipt(caller)
    caller.receipt_path.write_text(json.dumps(caller.receipt), encoding="utf-8")
    before = caller.receipt_path.read_bytes()
    runtime = mock.Mock()
    runtime.frozen_binding.side_effect = RuntimeError("backend identity changed")
    with (
        mock.patch.object(followup.FOLLOWUP, "client_runtime", return_value=runtime),
        mock.patch.object(followup.FOLLOWUP, "Proxy") as connect,
        contextlib.redirect_stdout(io.StringIO()),
    ):
        assert (
            followup.FOLLOWUP.main(["status", "--receipt", str(caller.receipt_path)])
            == 2
        )
    connect.assert_not_called()
    assert caller.receipt_path.read_bytes() == before


def test_native_followup_completion_keeps_frontend_anchor_separate() -> None:
    with ExitStack() as stack:
        fixture: Any = completion.NativeCompletionTests()
        fixture.addCleanup = stack.callback
        fixture.setUp()
        fixture.entry["frontend_message_id"] = "input"
        fixture.entry["client_message_id"] = "native-followup"
        fixture.entry["native_turn_id"] = "followup-turn"
        fixture.event("task_complete")
        fixture.event("task_started", turn="followup-turn")
        fixture.event(
            "item_completed",
            turn="followup-turn",
            item={"type": "UserMessage", "client_id": "native-followup"},
        )
        guard = completion.cli_client.NativeCompletion(fixture.entry)
        assert not guard.poll()
        fixture.event("task_complete", turn="followup-turn")
        assert guard.poll()
        fixture.event("task_started", turn="later-native-turn")
        with pytest.raises(RuntimeError, match="new native turn"):
            guard.poll()


def test_exact_native_command_line_read_never_starts_a_shell() -> None:
    client = startup.SESSIONS[startup.Host.CODEX].client
    with mock.patch.object(
        client.subprocess, "run", side_effect=AssertionError("unexpected shell")
    ):
        arguments = client.process_arguments(os.getpid())
    assert arguments
    assert Path(arguments[0]).stem.casefold().startswith("python")


def load_attachment_source(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class Attachment:
    sessions: Any
    registration: Any
    store_module: Any
    record: dict[str, Any]
    handle: str

    def snapshot(self) -> dict[str, Any] | None:
        with self.store_module.SessionStore() as store:
            session = store.get_session(self.handle)
            return None if session is None else asdict(session)

    def attach(self) -> Any:
        return self.sessions.handoff_recipient(self.record, self.registration, None)

    def change(self, changed: str) -> None:
        with self.store_module.SessionStore() as store:
            if changed == "missing":
                store.delete_session("thread")
            elif changed == "ended":
                store.update_session(
                    self.handle,
                    lambda session: session.end(
                        "handed_off", successor="codex:successor"
                    ),
                )
            else:
                store.update_session(
                    self.handle,
                    lambda session: setattr(
                        session, "profile", session.profile + "-other"
                    ),
                )


@pytest.fixture
def attachment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Attachment:
    root = Path(__file__).resolve().parents[1]
    directory = tmp_path / "attachment checkout café"
    directory.mkdir()
    monkeypatch.syspath_prepend(str(root / "plugins/agent/.agents/agent/runtime"))
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(directory / "state"))
    monkeypatch.setenv("CODEX_HOME", str(directory / "profile"))
    sessions = load_attachment_source(
        "owned_attachment_sessions", root / ".codex/runtime/sessions.py"
    )
    registration = load_attachment_source(
        "owned_attachment_registration",
        root / ".agents/agent/utility/peer/scripts/register_session.py",
    )
    store_module = load_attachment_source(
        "owned_attachment_store", root / ".agents/agent-store/runtime/session_store.py"
    )
    record: dict[str, Any] = {
        "token": "a" * 64,
        "backend": {"kind": "handoff", "pid": 103},
        "frontend": {"pid": 104, "started_at": 104.0},
        "recipient": {"thread_id": "thread", "cwd": str(directory)},
        "profile": str(directory / "profile"),
        "launch_cwd": str(directory),
        "recording_path": str(directory / "events.jsonl"),
    }
    submitted = {
        "turn_id": "native-turn",
        "client_message_id": "latest-native-message",
        "frontend_message_id": "frontend-anchor",
        "native_rollout": str(directory / "rollout.jsonl"),
        "native_cwd": str(directory),
    }
    monkeypatch.setattr(
        sessions.client, "bind_recipient", lambda *_args: (record, submitted)
    )
    monkeypatch.setattr(registration, "process_table", dict)
    monkeypatch.setattr(registration, "build_process_lookup", lambda: lambda _pid: None)
    monkeypatch.setattr(registration, "_load_session_store", lambda: store_module)
    with store_module.SessionStore() as store:
        handle = store.start_session(
            "codex", "thread", str(directory), pid=11, pid_started_at=11.0
        )
        store.upsert_work_item(
            "fixture-work-item",
            "fixture/repository",
            "feature",
            "item",
            status="active",
        )
        store.claim_work_item(handle, "fixture-work-item", "owner")
        store.update_session(handle, lambda session: session.stopped())
    return Attachment(sessions, registration, store_module, record, str(handle))


def test_recipient_attachment_preserves_responsibility_turn_and_lifecycle(
    attachment: Attachment,
) -> None:
    before = attachment.snapshot()
    assert before is not None
    assert before["role"] == "owner"
    assert before["work_item"] == "fixture-work-item"
    assert before["turn_state"] == "awaiting_input"
    result = attachment.attach()
    after = attachment.snapshot()
    assert after == {**before, "pid": 104, "pid_started_at": 104.0}
    assert result["recipient"] == attachment.record["recipient"]


@pytest.mark.parametrize("changed", ["missing", "ended", "profile"])
def test_recipient_attachment_refuses_ineligible_registration_without_changing_it(
    attachment: Attachment, changed: str
) -> None:
    attachment.change(changed)
    before = attachment.snapshot()
    with pytest.raises(RuntimeError, match=r"Agent Store|registration"):
        attachment.attach()
    assert attachment.snapshot() == before


@pytest.mark.parametrize("changed", ["missing", "ended", "profile"])
def test_attachment_transaction_does_not_revive_or_reassign_a_changed_registration(
    attachment: Attachment, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    record_start = attachment.registration._record_session_start
    expected: list[dict[str, Any] | None] = []

    def changed_before_transaction(*args: Any, **kwargs: Any) -> Any:
        attachment.change(changed)
        expected.append(attachment.snapshot())
        return record_start(*args, **kwargs)

    monkeypatch.setattr(
        attachment.registration, "_record_session_start", changed_before_transaction
    )
    with pytest.raises(RuntimeError, match=r"Agent Store|registration"):
        attachment.attach()
    assert len(expected) == 1
    assert attachment.snapshot() == expected[0]


class PeerReadError(OSError):
    winerror: int


@pytest.mark.parametrize("winerror", [6, 87, 299])
@pytest.mark.parametrize("state", ["absent", "reused", "live", "unknown"])
def test_partial_peer_read_requires_fresh_identity_evidence(
    monkeypatch: pytest.MonkeyPatch, winerror: int, state: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "plugins/agent/.agents/agent/runtime"))
    client = load_attachment_source(
        "owned_peer_read_client", root / ".codex/runtime/client.py"
    )
    peer = SimpleNamespace(pid=123, started_at=10.0)
    error = PeerReadError("peer process memory is unavailable")
    error.winerror = winerror
    current = SimpleNamespace(pid=123, started_at=11.0 if state == "reused" else 10.0)
    observed = ("present", current) if state in ("live", "reused") else (state, None)
    lookup = mock.Mock(return_value=observed)
    with mock.patch.object(client, "process_context", side_effect=error):
        if state in ("absent", "reused"):
            assert client.peer_recording_path(peer, lookup) is None
        else:
            with pytest.raises(OSError, match="peer process memory") as raised:
                client.peer_recording_path(peer, lookup)
            assert raised.value is error
    lookup.assert_called_once_with(peer.pid)


def test_peer_access_denied_is_never_treated_as_process_disappearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "plugins/agent/.agents/agent/runtime"))
    client = load_attachment_source(
        "owned_peer_access_client", root / ".codex/runtime/client.py"
    )
    error = PeerReadError("peer process access denied")
    error.winerror = 5
    lookup = mock.Mock(return_value=("absent", None))
    with (
        mock.patch.object(client, "process_context", side_effect=error),
        pytest.raises(PeerReadError, match="access denied") as raised,
    ):
        client.peer_recording_path(SimpleNamespace(pid=123, started_at=10.0), lookup)
    assert raised.value is error
    lookup.assert_not_called()
