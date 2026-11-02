from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any
from unittest import mock

import pytest
from close_fixtures import LIFECYCLE, CloseFixture, Host, session_close


@pytest.fixture
def session() -> Iterator[Any]:
    with ExitStack() as stack:
        fixture: Any = CloseFixture()
        fixture.addCleanup = stack.callback
        fixture.setup_close()
        yield fixture


def prepare(session: Any) -> dict[str, Any]:
    return LIFECYCLE.prepare(
        Host.CODEX,
        "one",
        session.profile,
        "task-completion",
        session_close,
        session.receipt(),
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
    with mock.patch.object(LIFECYCLE.secrets, "token_urlsafe", side_effect=("first", "second")):
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
