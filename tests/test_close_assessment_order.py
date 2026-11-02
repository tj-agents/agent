from collections.abc import Iterator
from contextlib import ExitStack
from itertools import count
from typing import Any, cast
from unittest import mock

import pytest
from close_fixtures import LIFECYCLE, CloseFixture, Host, session_close


@pytest.fixture
def session() -> Iterator[Any]:
    with ExitStack() as stack:
        fixture: Any = CloseFixture()
        fixture.addCleanup = stack.callback
        fixture.setup_close()
        tokens = count()
        stack.enter_context(
            mock.patch.object(
                LIFECYCLE.secrets,
                "token_urlsafe",
                side_effect=lambda _size: f"token-{next(tokens)}",
            )
        )
        yield fixture


def prepare(session: Any) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        LIFECYCLE.prepare(
            Host.CODEX,
            "one",
            session.profile,
            "task-completion",
            session_close,
            session.receipt(),
        ),
    )


def test_keep_open_policy_during_attribution_prevents_preparation(session: Any) -> None:
    adapter = session_close.host_sessions("codex")
    original = adapter.arm_close

    def keep_open(*args: Any) -> Any:
        LIFECYCLE.policy(
            Host.CODEX, "one", session.profile, "keep-open", "preserve this session"
        )
        return original(*args)

    with (
        mock.patch.object(adapter, "arm_close", side_effect=keep_open),
        pytest.raises(ValueError, match="keep-open"),
    ):
        prepare(session)

    assert not LIFECYCLE._state_path("one").exists()
    session.spawn.assert_not_called()


def test_new_input_cancels_a_prepared_generation_before_an_arm(session: Any) -> None:
    prepared = prepare(session)
    LIFECYCLE.run("prompt", Host.CODEX, {"session_id": "one", "turn_id": "turn-two"})
    with pytest.raises(ValueError, match="not prepared"):
        LIFECYCLE.arm(
            Host.CODEX,
            "one",
            session.profile,
            prepared["release_generation"],
            session_close,
        )
    session.spawn.assert_not_called()


def test_only_the_current_release_generation_can_arm(session: Any) -> None:
    with mock.patch.object(
        LIFECYCLE.secrets, "token_urlsafe", side_effect=("first", "second")
    ):
        first = prepare(session)
        second = prepare(session)
    with pytest.raises(ValueError, match="generation changed"):
        LIFECYCLE.recheck(
            Host.CODEX,
            "one",
            session.profile,
            first["release_generation"],
            session_close,
        )
    outcome = LIFECYCLE.arm(
        Host.CODEX,
        "one",
        session.profile,
        second["release_generation"],
        session_close,
    )
    assert outcome["status"] == "armed"
    assert session.spawn.call_count == 1


def prompt(session: Any) -> None:
    LIFECYCLE.run("prompt", Host.CODEX, {"session_id": "one", "turn_id": "turn-two"})


def keep_open(session: Any) -> None:
    LIFECYCLE.policy(
        Host.CODEX, "one", session.profile, "keep-open", "preserve this session"
    )


def arm(session: Any, prepared: dict[str, Any]) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        LIFECYCLE.arm(
            Host.CODEX,
            "one",
            session.profile,
            prepared["release_generation"],
            session_close,
        ),
    )


@pytest.mark.parametrize("change", [prompt, keep_open])
def test_recheck_cannot_restore_a_cancelled_generation(
    session: Any, change: Any
) -> None:
    prepared = prepare(session)
    original = LIFECYCLE._read(LIFECYCLE._state_path("one"))
    validate = LIFECYCLE._validate_frozen_receipt

    def changed(*args: Any) -> None:
        change(session)
        validate(*args)

    with (
        mock.patch.object(LIFECYCLE, "_validate_frozen_receipt", side_effect=changed),
        pytest.raises(ValueError),
    ):
        LIFECYCLE.recheck(
            Host.CODEX,
            "one",
            session.profile,
            prepared["release_generation"],
            session_close,
        )

    state = LIFECYCLE._read(LIFECYCLE._state_path("one"))
    assert state["status"] == "cancelled"
    assert state["native"] == original["native"]
    assert state["receipt"] == original["receipt"]
    assert LIFECYCLE.Path(state["cancel"]).exists()
    session.spawn.assert_not_called()


@pytest.mark.parametrize("change", [prompt, keep_open])
def test_arm_token_generation_cannot_restore_cancellation(
    session: Any, change: Any
) -> None:
    prepared = prepare(session)

    def changed(_size: int) -> str:
        with mock.patch.object(
            LIFECYCLE.secrets, "token_urlsafe", return_value="new-input"
        ):
            change(session)
        return "stale-arm"

    with (
        mock.patch.object(LIFECYCLE.secrets, "token_urlsafe", side_effect=changed),
        pytest.raises(ValueError),
    ):
        arm(session, prepared)

    assert LIFECYCLE._read(LIFECYCLE._state_path("one"))["status"] == "cancelled"
    session.spawn.assert_not_called()


@pytest.mark.parametrize("change", [prompt, keep_open])
def test_cancellation_during_acknowledgement_is_observed(
    session: Any, change: Any
) -> None:
    prepared = prepare(session)

    def changed(script: Any, arguments: Any) -> int:
        change(session)
        return int(session.acknowledge_watcher(script, arguments))

    session.spawn.side_effect = changed
    with pytest.raises(ValueError):
        arm(session, prepared)

    state = LIFECYCLE._read(LIFECYCLE._state_path("one"))
    assert state["status"] == "cancelled"
    assert LIFECYCLE.Path(state["cancel"]).exists()
    assert session.spawn.call_count == 1


@pytest.mark.parametrize("change", [prompt, keep_open])
def test_prepare_keeps_its_input_binding_during_receipt_validation(
    session: Any, change: Any
) -> None:
    validate = LIFECYCLE._validate_receipt

    def changed(*args: Any, **kwargs: Any) -> Any:
        change(session)
        return validate(*args, **kwargs)

    with (
        mock.patch.object(LIFECYCLE, "_validate_receipt", side_effect=changed),
        pytest.raises(ValueError),
    ):
        prepare(session)

    assert not LIFECYCLE._state_path("one").exists()
    session.spawn.assert_not_called()


def test_explicit_cancel_during_initial_prepare_is_not_lost(session: Any) -> None:
    validate = LIFECYCLE._validate_receipt

    def changed(*args: Any, **kwargs: Any) -> Any:
        LIFECYCLE.cancel(Host.CODEX, "one", "cancel during assessment")
        return validate(*args, **kwargs)

    with (
        mock.patch.object(LIFECYCLE, "_validate_receipt", side_effect=changed),
        pytest.raises(ValueError, match="generation changed"),
    ):
        prepare(session)

    assert not LIFECYCLE._state_path("one").exists()


@pytest.mark.parametrize("operation", ["prepare", "recheck", "arm"])
def test_superseding_prepare_wins_over_stale_publication(
    session: Any, operation: str
) -> None:
    prepared = prepare(session) if operation != "prepare" else None
    successor: dict[str, Any] = {}
    hook = "_validate_receipt" if operation == "prepare" else "_validate_frozen_receipt"
    validate = getattr(LIFECYCLE, hook)

    def changed(*args: Any, **kwargs: Any) -> Any:
        with mock.patch.object(LIFECYCLE, hook, side_effect=validate):
            successor.update(
                LIFECYCLE.prepare(
                    Host.CODEX,
                    "one",
                    session.profile,
                    "task-completion",
                    session_close,
                    session.root / "terminal-acceptance.json",
                )
            )
        return validate(*args, **kwargs)

    with (
        mock.patch.object(LIFECYCLE, hook, side_effect=changed),
        pytest.raises(ValueError, match="generation changed"),
    ):
        if operation == "prepare":
            prepare(session)
        elif operation == "recheck":
            assert prepared is not None
            LIFECYCLE.recheck(
                Host.CODEX,
                "one",
                session.profile,
                prepared["release_generation"],
                session_close,
            )
        else:
            assert prepared is not None
            arm(session, prepared)

    state = LIFECYCLE._read(LIFECYCLE._state_path("one"))
    assert state["status"] == "prepared"
    assert state["release_generation"] == successor["release_generation"]
    session.spawn.assert_not_called()


def test_failed_stale_watcher_does_not_cancel_its_successor(session: Any) -> None:
    prepared = prepare(session)
    successor: dict[str, Any] = {}

    def changed(_script: Any, _arguments: Any) -> int:
        successor.update(prepare(session))
        raise RuntimeError("old watcher launch failed")

    session.spawn.side_effect = changed
    with pytest.raises(RuntimeError, match="old watcher"):
        arm(session, prepared)

    state = LIFECYCLE._read(LIFECYCLE._state_path("one"))
    assert state["status"] == "prepared"
    assert state["release_generation"] == successor["release_generation"]
    session.spawn.side_effect = session.acknowledge_watcher
    assert arm(session, successor)["status"] == "armed"


def test_nested_arm_can_launch_only_one_watcher(session: Any) -> None:
    prepared = prepare(session)
    tokens = LIFECYCLE.secrets.token_urlsafe
    successor: dict[str, Any] = {}

    def changed(size: int) -> str:
        with mock.patch.object(LIFECYCLE.secrets, "token_urlsafe", side_effect=tokens):
            successor.update(arm(session, prepared))
        return "stale-arm"

    with (
        mock.patch.object(LIFECYCLE.secrets, "token_urlsafe", side_effect=changed),
        pytest.raises(ValueError, match="generation changed"),
    ):
        arm(session, prepared)

    assert successor["status"] == "armed"
    assert session.spawn.call_count == 1


def test_session_lock_has_a_bounded_wait(session: Any) -> None:
    with (
        LIFECYCLE._session_lock("one"),
        mock.patch.object(LIFECYCLE, "STATE_LOCK_TIMEOUT_SECONDS", 0.02),
        pytest.raises(RuntimeError, match="state lock"),
    ):
        LIFECYCLE.cancel(Host.CODEX, "one", "contended")


@pytest.mark.parametrize("change", [prompt, keep_open])
def test_cancellation_inside_acknowledgement_validation_is_observed(
    session: Any, change: Any
) -> None:
    prepared = prepare(session)
    acknowledged = LIFECYCLE._acknowledged

    def changed(*args: Any) -> bool:
        result = bool(acknowledged(*args))
        assert result
        change(session)
        return result

    with (
        mock.patch.object(LIFECYCLE, "_acknowledged", side_effect=changed),
        pytest.raises(ValueError),
    ):
        arm(session, prepared)

    state = LIFECYCLE._read(LIFECYCLE._state_path("one"))
    assert state["status"] == "cancelled"
    assert LIFECYCLE.Path(state["cancel"]).exists()
    assert session.spawn.call_count == 1
