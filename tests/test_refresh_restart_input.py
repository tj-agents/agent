import os
from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any
from unittest import mock

import pytest
from close_fixtures import (
    LIFECYCLE,
    WATCHER,
    CloseFixture,
    Host,
    cli_client,
    session_close,
)


@pytest.fixture
def session() -> Iterator[Any]:
    with ExitStack() as stack:
        fixture: Any = CloseFixture()
        fixture.addCleanup = stack.callback
        fixture.setup_close()
        LIFECYCLE._input_path("one").unlink()
        fixture.commands[20] = ["codex", "--no-daemon"]
        fixture.backend = None
        record = LIFECYCLE._read(fixture.recording.with_name("client.json"))
        fixture.write(
            fixture.recording.with_name("client.json"), {**record, "backend": None}
        )
        yield fixture


def restart(session: Any) -> dict[str, Any]:
    outcome: dict[str, Any] = LIFECYCLE.assess(
        Host.CODEX,
        "one",
        session.profile,
        "restart",
        "adopt verified refreshed plugins",
        session_close,
    )
    if outcome["status"] == "armed":
        session.request_path = session.root / "close-lifecycle/requests/token.json"
        session.digest = outcome["request_sha256"]
        session.request = WATCHER.load_request(session.request_path, session.digest)
    return outcome


def test_standalone_restart_does_not_need_new_hook_receipt(session: Any) -> None:
    assert restart(session)["status"] == "armed"
    assert not LIFECYCLE._input_path("one").exists()
    session.append_rollout(
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-one"},
        }
    )
    assert session.wait() == 0
    assert session.outcome()["status"] == "closed"
    session.primitive_stop.assert_called_once()


def test_completion_still_requires_hook_receipt(session: Any) -> None:
    assert session.assess("complete") == 2
    session.spawn.assert_not_called()


def test_new_input_without_new_hooks_cancels_restart(session: Any) -> None:
    assert restart(session)["status"] == "armed"
    session.append_rollout(
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-one"},
        }
    )
    session.recording.write_text(
        session.recording.read_text()
        + '{"dir":"from_tui","kind":"op","payload":{"UserTurn":'
        '{"client_user_message_id":"input-two"}}}\n',
        encoding="utf-8",
    )
    assert session.wait() == 0
    assert session.outcome()["status"] == "cancelled"
    session.primitive_stop.assert_not_called()


def test_ambiguous_frontends_cannot_restart(session: Any) -> None:
    session.infos[30] = session.infos[20]._replace(pid=30)
    session.contexts[30] = session.contexts[20]
    with pytest.raises(RuntimeError, match="Multiple native processes"):
        restart(session)
    session.spawn.assert_not_called()


def test_later_keep_open_cancels_restart(session: Any) -> None:
    assert restart(session)["status"] == "armed"
    LIFECYCLE.run("prompt", Host.CODEX, {"session_id": "one", "turn_id": "turn-one"})
    assert session.assess("keep-open") == 0
    assert session.wait() == 0
    assert session.outcome()["status"] == "cancelled"
    session.primitive_stop.assert_not_called()


def test_later_keep_open_without_hook_receipt_cancels_restart(
    session: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert restart(session)["status"] == "armed"
    assert session.assess("keep-open") == 0
    assert not LIFECYCLE._input_path("one").exists()
    capsys.readouterr()
    session.stop()
    assert capsys.readouterr().out == ""
    assert LIFECYCLE.assessed(Host.CODEX, "one", "turn-one", {})
    assert not LIFECYCLE.assessed(Host.CODEX, "one", "turn-two", {})
    assert session.wait() == 0
    assert session.outcome()["status"] == "cancelled"
    session.primitive_stop.assert_not_called()


def test_wrong_profile_cannot_cancel_restart(session: Any) -> None:
    assert restart(session)["status"] == "armed"
    with pytest.raises(ValueError, match="different profile"):
        LIFECYCLE.assess(
            Host.CODEX,
            "one",
            session.root / "another-profile",
            "keep-open",
            "different profile",
            session_close,
        )
    assert not (session.root / "close-lifecycle/requests/token.cancel.json").exists()


def test_new_stop_preserves_a_restart_prepared_without_hooks(session: Any) -> None:
    assert restart(session)["status"] == "armed"
    assessment = LIFECYCLE._read(LIFECYCLE.state_root() / "assessments/one.json")
    session.stop()
    assert LIFECYCLE.assessed(Host.CODEX, "one", "turn-one", {})
    assert (
        LIFECYCLE._read(LIFECYCLE.state_root() / "assessments/one.json") == assessment
    )
    assert session.wait() == 0
    assert session.outcome()["status"] == "closed"


def test_keep_open_during_restart_attribution_wins(session: Any) -> None:
    original = LIFECYCLE.restart_input

    def preserve(*args: Any) -> Any:
        with (
            mock.patch.object(LIFECYCLE, "restart_input", original),
            mock.patch.dict("sys.modules", {"close_lifecycle": LIFECYCLE}),
        ):
            assert session.assess("keep-open", "still working") == 0
        return original(*args)

    with mock.patch.object(LIFECYCLE, "restart_input", side_effect=preserve):
        assert restart(session)["status"] == "unavailable"
    assessment = LIFECYCLE._read(LIFECYCLE.state_root() / "assessments/one.json")
    assert assessment["status"] == "kept-open"
    assert assessment["reason"] == "still working"
    session.spawn.assert_not_called()


def test_frontend_switch_during_publication_cannot_restart(session: Any) -> None:
    adapter = session_close.host_sessions("codex")
    original = adapter.arm_close

    def switch(*args: Any) -> Any:
        result = original(*args)
        with mock.patch.object(cli_client, "latest_input", return_value="input-two"):
            LIFECYCLE.require_input(Host.CODEX, "one", args[2], session_close)
        return result

    with mock.patch.object(adapter, "arm_close", side_effect=switch):
        assert restart(session)["status"] == "unavailable"
    session.spawn.assert_not_called()


def test_same_frontend_registration_refresh_preserves_restart(session: Any) -> None:
    adapter = session_close.host_sessions("codex")
    original = adapter.arm_close

    def update_registration(*args: Any) -> Any:
        result = original(*args)
        record = LIFECYCLE._read(session.registry)
        record["started_at"] += 1
        if os.name == "nt":
            record["cwd"] = record["cwd"].swapcase()
        session.write(session.registry, record)
        return result

    with mock.patch.object(adapter, "arm_close", side_effect=update_registration):
        assert restart(session)["status"] == "armed"
    session.append_rollout(
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-one"},
        }
    )
    assert session.wait() == 0
    assert session.outcome()["status"] == "closed"


@pytest.mark.parametrize("new_input", [False, True])
@pytest.mark.parametrize("delayed_stop", [False, True])
def test_claude_restart_uses_native_transcript_without_new_hooks(
    new_input: bool,
    delayed_stop: bool,
) -> None:
    with ExitStack() as stack:
        session: Any = CloseFixture()
        session.addCleanup = stack.callback
        session.setup_close("claude")
        LIFECYCLE._input_path("one").unlink()
        adapter = session_close.host_sessions("claude")
        stack.enter_context(
            mock.patch.object(adapter, "transcript", return_value=session.transcript)
        )
        outcome = LIFECYCLE.assess(
            Host.CLAUDE,
            "one",
            session.profile,
            "restart",
            "adopt verified refreshed plugins",
            session_close,
        )
        assert outcome["status"] == "armed"
        session.request_path = session.root / "close-lifecycle/requests/token.json"
        session.digest = outcome["request_sha256"]
        session.request = WATCHER.load_request(session.request_path, session.digest)
        session.append({"type": "assistant", "message": {"stop_reason": "end_turn"}})
        recorded = False

        def record_stop(_elapsed: float = 0) -> None:
            nonlocal recorded
            if not recorded:
                session.store.record_stop(
                    session.handle, session.store_module.Checkout.probe(session.root)
                )
                recorded = True

        if not delayed_stop:
            record_stop()
        if new_input:
            session.append(
                {"type": "user", "uuid": "user-two", "message": {"content": "wait"}}
            )
        assert session.wait(record_stop if delayed_stop else None) == 0
        assert session.outcome()["status"] == ("cancelled" if new_input else "closed")
        assert session.primitive_stop.call_count == (0 if new_input else 1)
