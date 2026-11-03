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


def test_later_keep_open_during_attribution_prevents_publication(session: Any) -> None:
    adapter = session_close.host_sessions("codex")
    original = adapter.arm_close

    def keep_open(*args: Any) -> Any:
        assert session.assess("keep-open", "preserve this session") == 0
        return original(*args)

    with mock.patch.object(adapter, "arm_close", side_effect=keep_open):
        assert session.assess("complete") == 2

    assessment = LIFECYCLE._read(LIFECYCLE.state_root() / "assessments/one.json")
    assert assessment["status"] == "kept-open"
    assert assessment["reason"] == "preserve this session"
    assert not (session.root / "close-lifecycle/one.json").exists()
    session.spawn.assert_not_called()


def test_later_keep_open_survives_an_earlier_attribution_failure(session: Any) -> None:
    adapter = session_close.host_sessions("codex")

    def keep_open_then_fail(*args: Any) -> Any:
        assert session.assess("keep-open", "preserve this session") == 0
        raise RuntimeError("attribution failed")

    with mock.patch.object(adapter, "arm_close", side_effect=keep_open_then_fail):
        assert session.assess("complete") == 2

    assessment = LIFECYCLE._read(LIFECYCLE.state_root() / "assessments/one.json")
    assert assessment["status"] == "kept-open"
    session.spawn.assert_not_called()


def test_repeated_complete_preserves_the_armed_assessment(session: Any) -> None:
    session.arm()
    original = LIFECYCLE._read(LIFECYCLE.state_root() / "assessments/one.json")
    assert session.assess("complete") == 2
    assert LIFECYCLE._read(LIFECYCLE.state_root() / "assessments/one.json") == original
    assert LIFECYCLE.assessed(Host.CODEX, "one", "turn-one", {})
    session.stop()
    assert session.wait() == 0
    assert session.outcome()["status"] == "closed"
    session.primitive_stop.assert_called_once()


def test_new_input_records_assessment_while_cancelled_request_remains(
    session: Any,
) -> None:
    session.arm()
    LIFECYCLE.run("prompt", Host.CODEX, {"session_id": "one", "turn_id": "turn-two"})
    assert session.assess("complete") == 2
    assert LIFECYCLE.assessed(Host.CODEX, "one", "turn-two", {})
    assert session.wait() == 0
    assert session.outcome()["status"] == "cancelled"
    assert LIFECYCLE.assessed(Host.CODEX, "one", "turn-two", {})
    session.primitive_stop.assert_not_called()
