import importlib
import json
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from itertools import count
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from close_fixtures import LIFECYCLE, WATCHER, CloseFixture, Host, session_close


@pytest.fixture
def session(request: pytest.FixtureRequest) -> Iterator[Any]:
    with ExitStack() as stack:
        fixture: Any = CloseFixture()
        fixture.addCleanup = stack.callback
        mode = getattr(request, "param", "codex")
        fixture.setup_close("claude" if mode == "claude" else "codex")
        if mode == "private":
            fixture.commands[20] = ["codex", "--no-daemon"]
            ticket = fixture.recording.with_name("client.json")
            value = json.loads(ticket.read_text(encoding="utf-8"))
            fixture.write(ticket, {**value, "backend": None})
        if mode == "claude":
            transcript = fixture.profile / "projects/project/one.jsonl"
            transcript.parent.mkdir(parents=True)
            fixture.transcript.replace(transcript)
            fixture.transcript = transcript
        LIFECYCLE._input_path("one").unlink()
        tokens = count()
        stack.enter_context(
            mock.patch.object(
                LIFECYCLE.secrets,
                "token_urlsafe",
                side_effect=lambda _size: f"refresh-{next(tokens)}",
            )
        )
        yield fixture


def prepare(session: Any) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        LIFECYCLE.prepare(
            Host(session.host),
            "one",
            session.profile,
            "refresh-restart",
            session_close,
        ),
    )


def arm(session: Any, prepared: dict[str, Any] | None = None) -> dict[str, Any]:
    prepared = prepared or prepare(session)
    LIFECYCLE.recheck(
        Host(session.host),
        "one",
        session.profile,
        prepared["release_generation"],
        session_close,
    )
    armed = cast(
        dict[str, Any],
        LIFECYCLE.arm(
            Host(session.host),
            "one",
            session.profile,
            prepared["release_generation"],
            session_close,
        ),
    )
    session.request_path = Path(armed["request"])
    session.digest = armed["request_sha256"]
    session.request = WATCHER.load_request(session.request_path, session.digest)
    return armed


@pytest.mark.parametrize("session", ["codex", "private", "claude"], indirect=True)
def test_missing_hook_refresh_runs_prepare_recheck_arm_and_watcher(
    session: Any,
) -> None:
    arm(session)
    assert session.request["input_hook"] is None
    session.stop()
    assert session.wait() == 0
    assert session.outcome()["status"] == "closed"
    assert not LIFECYCLE._input_path("one").exists()
    assert session.primitive_stop.call_count == 1


def native_input(session: Any) -> None:
    if session.host == "claude":
        session.append(
            {"type": "user", "uuid": "user-two", "message": {"content": "continue"}}
        )
        return
    session.native_input = "input-two"
    with session.recording.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "dir": "from_tui",
                    "kind": "op",
                    "payload": {"UserTurn": {"client_user_message_id": "input-two"}},
                }
            )
            + "\n"
        )


@pytest.mark.parametrize("session", ["codex", "claude"], indirect=True)
@pytest.mark.parametrize("intent", ["task-completion", "user-dismissal"])
def test_other_release_intents_still_require_the_hook(
    session: Any, intent: str
) -> None:
    with pytest.raises(ValueError, match="input hook receipt"):
        LIFECYCLE.prepare(
            Host(session.host),
            "one",
            session.profile,
            intent,
            session_close,
            session.receipt(),
            dismissal_reason="user asked to close",
        )
    session.spawn.assert_not_called()


@pytest.mark.parametrize("session", ["codex", "claude"], indirect=True)
@pytest.mark.parametrize("stage", ["attribution", "recheck", "arm", "acknowledgement"])
def test_unhooked_native_input_change_prevents_release(
    session: Any, stage: str
) -> None:
    adapter = session_close.host_sessions(session.host)
    if stage == "attribution":
        original = adapter.arm_close

        def attribute(*args: Any) -> Any:
            result = original(*args)
            native_input(session)
            return result

        with (
            mock.patch.object(adapter, "arm_close", side_effect=attribute),
            pytest.raises((ValueError, RuntimeError)),
        ):
            prepare(session)
        assert not LIFECYCLE._state_path("one").exists()
    else:
        prepared = prepare(session)
        if stage == "recheck":
            native_input(session)
            with pytest.raises((ValueError, RuntimeError)):
                arm(session, prepared)
        elif stage == "arm":
            original_recheck = LIFECYCLE.recheck

            def recheck(*args: Any) -> Any:
                result = original_recheck(*args)
                native_input(session)
                return result

            with (
                mock.patch.object(LIFECYCLE, "recheck", side_effect=recheck),
                pytest.raises((ValueError, RuntimeError)),
            ):
                LIFECYCLE.arm(
                    Host(session.host),
                    "one",
                    session.profile,
                    prepared["release_generation"],
                    session_close,
                )
        else:
            original_acknowledged = LIFECYCLE._acknowledged

            def acknowledged(*args: Any) -> bool:
                accepted = bool(original_acknowledged(*args))
                native_input(session)
                return accepted

            with (
                mock.patch.object(LIFECYCLE, "_acknowledged", side_effect=acknowledged),
                pytest.raises((ValueError, RuntimeError)),
            ):
                arm(session, prepared)
            state = LIFECYCLE._read(LIFECYCLE._state_path("one"))
            assert state["status"] == "cancelled"
            assert Path(state["cancel"]).exists()
    assert not LIFECYCLE._input_path("one").exists()
    session.primitive_stop.assert_not_called()


@pytest.mark.parametrize("session", ["codex", "claude"], indirect=True)
def test_unhooked_native_input_change_at_final_signal_preserves_frontend(
    session: Any,
) -> None:
    arm(session)
    session.stop()
    signals: list[object] = []

    def stop(identity: object, *, before_signal: Any) -> None:
        native_input(session)
        before_signal()
        signals.append(identity)

    with mock.patch.object(session.primitive_stop, "side_effect", stop):
        assert session.wait() == 0
    assert signals == []
    assert session.outcome()["status"] == "cancelled"
    assert Path(session.request["cancel"]).exists()


@pytest.mark.parametrize("session", ["codex", "claude"], indirect=True)
@pytest.mark.parametrize("mutation", ["keep-open", "prompt", "replacement"])
def test_policy_input_and_replacement_win_during_refresh_attribution(
    session: Any, mutation: str
) -> None:
    adapter = session_close.host_sessions(session.host)
    original = adapter.arm_close
    fired = False

    def attribute(*args: Any) -> Any:
        nonlocal fired
        result = original(*args)
        if not fired:
            fired = True
            if mutation == "keep-open":
                LIFECYCLE.policy(
                    Host(session.host), "one", session.profile, "keep-open", "continue"
                )
            elif mutation == "prompt":
                LIFECYCLE.run(
                    "prompt",
                    Host(session.host),
                    {"session_id": "one", "turn_id": "two"},
                )
            else:
                prepare(session)
        return result

    with (
        mock.patch.object(adapter, "arm_close", side_effect=attribute),
        pytest.raises(ValueError),
    ):
        prepare(session)
    session.spawn.assert_not_called()


@pytest.mark.parametrize("session", ["codex", "claude"], indirect=True)
@pytest.mark.parametrize("recorded", [False, True])
def test_optional_hook_record_is_frozen_including_absence(
    session: Any, recorded: bool
) -> None:
    path = LIFECYCLE._input_path("one")
    if recorded:
        LIFECYCLE.run(
            "prompt",
            Host(session.host),
            {
                "session_id": "one",
                "turn_id": "turn-one",
                "transcript_path": str(session.transcript),
            },
        )
    prepared = prepare(session)
    frozen = LIFECYCLE._read(path)
    assert LIFECYCLE._read(LIFECYCLE._state_path("one"))["input_hook"] == frozen
    if recorded:
        path.unlink()
    else:
        session.write(path, {"input_generation": "different"})
    with pytest.raises(ValueError, match="native input changed"):
        arm(session, prepared)
    session.spawn.assert_not_called()


@pytest.mark.parametrize("session", ["codex", "claude"], indirect=True)
def test_same_binding_bookkeeping_refresh_keeps_original_frontend(session: Any) -> None:
    prepared = prepare(session)
    frozen = LIFECYCLE._read(LIFECYCLE._state_path("one"))["entry"]
    session.write(session.registry, {**frozen, "started_at": 99.0})
    arm(session, prepared)
    assert session.request["entry"] == frozen
    session.stop()
    assert session.wait() == 0
    assert session.outcome()["status"] == "closed"


@pytest.mark.parametrize("session", ["codex", "claude"], indirect=True)
@pytest.mark.parametrize("field", ["pid", "profile", "terminal", "provenance"])
def test_changed_registration_never_adopts_a_new_frontend(
    session: Any, field: str
) -> None:
    prepared = prepare(session)
    original = session_close.read_json(session.registry)
    session.write(session.registry, {**original, field: "changed"})
    with pytest.raises(ValueError, match="registration changed"):
        arm(session, prepared)
    session.spawn.assert_not_called()


@pytest.mark.parametrize(
    "field",
    [*session_close.CLIENT_BINDING_FIELDS, "frontend", "provenance", "terminal", "cwd"],
)
def test_registration_equality_preserves_every_identity_field(field: str) -> None:
    entry = {field: "original", "cwd": str(Path.cwd().resolve())}
    assert session_close.registration_matches({**entry, "started_at": 99.0}, entry)
    assert not session_close.registration_matches({**entry, field: "changed"}, entry)


@pytest.mark.parametrize("session", ["claude"], indirect=True)
@pytest.mark.parametrize("delay", [0.0, 0.1])
@pytest.mark.parametrize("new_input", [None, "before-stop", "after-stop"])
def test_claude_transcript_completion_waits_for_fresh_stop_store_state(
    session: Any, delay: float, new_input: str | None
) -> None:
    arm(session)
    session.append({"type": "assistant", "message": {"stop_reason": "end_turn"}})
    stopped = False
    changed = False

    def during(elapsed: float) -> None:
        nonlocal stopped, changed
        if new_input == "before-stop" and not changed:
            native_input(session)
            changed = True
        if elapsed >= delay and not stopped:
            session.store.record_stop(
                session.handle, session.store_module.Checkout.probe(session.root)
            )
            stopped = True
        if new_input == "after-stop" and stopped and not changed:
            native_input(session)
            changed = True

    assert session.wait(during) == 0
    assert not Path(session.request["completion"]).exists()
    assert session.outcome()["status"] == ("cancelled" if new_input else "closed")
    assert session.primitive_stop.call_count == (0 if new_input else 1)


@pytest.mark.parametrize("session", ["claude"], indirect=True)
def test_claude_finished_transcript_without_stop_preserves_working_session(
    session: Any,
) -> None:
    arm(session)
    session.append({"type": "assistant", "message": {"stop_reason": "end_turn"}})
    assert session.wait() == 2
    assert session.outcome()["status"] == "timeout"
    session.primitive_stop.assert_not_called()
    assert Path(session.request["cancel"]).exists()


@pytest.mark.parametrize("session", ["codex", "claude"], indirect=True)
@pytest.mark.parametrize("changed", [False, True])
def test_real_relaunch_arm_preserves_its_original_binding(
    session: Any, changed: bool
) -> None:
    relaunch = importlib.import_module("session_relaunch")
    adapter = session_close.host_sessions(session.host)
    entry = adapter.prepared_record(
        session_close.read_json(session.registry), "one", session_close
    )
    if changed:
        session.write(session.registry, {**entry, "pid": 99})
    with mock.patch.dict(sys.modules, {"close_lifecycle": LIFECYCLE}):
        if changed:
            with pytest.raises(ValueError, match="registration changed"):
                relaunch.arm_client_close(
                    session.root, entry, session_close, Host(session.host)
                )
            session.spawn.assert_not_called()
            return
        armed = relaunch.arm_client_close(
            session.root, entry, session_close, Host(session.host)
        )
    session.request_path = Path(armed["request"])
    session.digest = armed["request_sha256"]
    session.request = WATCHER.load_request(session.request_path, session.digest)
    session.stop()
    assert session.wait() == 0
    assert session.outcome()["status"] == "closed"
    assert not LIFECYCLE._input_path("one").exists()


@pytest.mark.parametrize("session", ["codex", "claude"], indirect=True)
def test_malformed_hook_record_is_not_treated_as_absence(session: Any) -> None:
    path = LIFECYCLE._input_path("one")
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="hook receipt is invalid"):
        prepare(session)
    session.spawn.assert_not_called()


@pytest.mark.parametrize("session", ["claude"], indirect=True)
def test_refresh_cannot_substitute_an_unbound_claude_transcript(session: Any) -> None:
    other = session.root / "other.jsonl"
    other.write_bytes(session.transcript.read_bytes())
    with pytest.raises(ValueError, match="transcript differs"):
        LIFECYCLE.prepare(
            Host.CLAUDE,
            "one",
            session.profile,
            "refresh-restart",
            session_close,
            supplied=other,
        )
    session.spawn.assert_not_called()


@pytest.mark.parametrize("session", ["codex", "claude"], indirect=True)
@pytest.mark.parametrize("stage", ["acknowledgement", "poll", "final-guard"])
def test_native_verification_error_cancels_without_signalling(
    session: Any, stage: str
) -> None:
    arm(session)
    session.stop()
    adapter = session_close.host_sessions(session.host)
    if stage == "acknowledgement":
        target, attribute = adapter, "restart_input"
    elif stage == "poll":
        target, attribute = adapter, "settled"
    else:
        target, attribute = adapter, "close_guard"
    with mock.patch.object(
        target, attribute, side_effect=OSError("native unavailable")
    ):
        assert session.wait() == 0
    assert session.outcome()["status"] == "cancelled"
    assert Path(session.request["cancel"]).exists()
    assert session.row().ended_at is None


@pytest.mark.parametrize("session", ["codex", "claude"], indirect=True)
@pytest.mark.parametrize("stage", ["acknowledgement", "poll", "final-signal"])
def test_changed_registry_binding_cannot_replace_the_armed_frontend(
    session: Any, stage: str
) -> None:
    if stage == "acknowledgement":
        original = LIFECYCLE._acknowledged

        def acknowledged(*args: Any) -> bool:
            accepted = bool(original(*args))
            entry = session_close.read_json(session.registry)
            session.write(session.registry, {**entry, "pid": 99})
            return accepted

        with (
            mock.patch.object(LIFECYCLE, "_acknowledged", side_effect=acknowledged),
            pytest.raises(ValueError, match="registration changed"),
        ):
            arm(session)
        state = LIFECYCLE._read(LIFECYCLE._state_path("one"))
        assert state["status"] == "cancelled"
        assert Path(state["cancel"]).exists()
        return
    arm(session)
    session.stop()
    entry = session.request["entry"]
    if stage == "poll":
        session.write(session.registry, {**entry, "pid": 99})
        assert session.wait() == 0
        session.primitive_stop.assert_not_called()
    else:
        signals: list[object] = []

        def stop(identity: object, *, before_signal: Any) -> None:
            session.write(session.registry, {**entry, "pid": 99})
            before_signal()
            signals.append(identity)

        with mock.patch.object(session.primitive_stop, "side_effect", stop):
            assert session.wait() == 0
        assert signals == []
    assert session.outcome()["status"] == "cancelled"
    assert session.row().ended_at is None
