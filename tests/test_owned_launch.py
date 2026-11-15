import subprocess
from collections.abc import Callable

import agent_store
import owned_launch
import pytest
from tj_cli import process

CURRENT = owned_launch.current

type Run = Callable[..., subprocess.CompletedProcess[str]]


def missing_tj(*_arguments: object, **_options: object) -> None:
    raise FileNotFoundError("tj")


def slow_tj(command: list[str], **options: object) -> None:
    assert options["timeout"] == owned_launch.TIMEOUT_SECONDS
    raise subprocess.TimeoutExpired(command, owned_launch.TIMEOUT_SECONDS)


def answered(returncode: int, stdout: str) -> Run:
    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        assert command == ["tj", "launch"]
        return subprocess.CompletedProcess(command, returncode, stdout, "boom")

    return run


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        pytest.param(answered(0, "launch-1\n"), "launch-1", id="owned"),
        pytest.param(answered(0, ""), None, id="not-owned"),
    ],
)
def test_owned_launch_is_only_what_tj_launch_prints(
    monkeypatch: pytest.MonkeyPatch,
    run: Callable[..., object],
    expected: str | None,
) -> None:
    monkeypatch.setattr(process, "run", run)
    assert CURRENT() == expected


@pytest.mark.parametrize(
    ("run", "message"),
    [
        pytest.param(answered(2, "launch-1\n"), "exited 2: boom", id="failed"),
        pytest.param(missing_tj, "did not answer", id="tj-missing"),
        pytest.param(slow_tj, "did not answer", id="timed-out"),
    ],
)
def test_owned_launch_raises_when_tj_launch_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
    run: Callable[..., object],
    message: str,
) -> None:
    monkeypatch.setattr(process, "run", run)
    with pytest.raises(owned_launch.OwnedLaunchError, match=message):
        CURRENT()


@pytest.mark.parametrize(
    ("bound", "host", "expected"),
    [
        (None, "codex", "L1"),
        ("codex:mine", "codex", "L1"),
        ("codex:other", "codex", None),
        (None, "claude", None),
    ],
)
def test_launch_admission_checks_the_native_session(
    monkeypatch: pytest.MonkeyPatch,
    bound: str | None,
    host: str,
    expected: str | None,
) -> None:
    def show(*arguments: str) -> dict[str, object]:
        assert arguments == ("show", "--launch-id", "L1")
        return {
            "launch": {
                "launch_id": "L1",
                "host": host,
                "state": "active",
                "session": bound,
            },
            "session": {
                "session": "codex:mine",
                "host": "codex",
                "native_id": "mine",
                "launch_id": "L1",
                "state": "active",
            },
        }

    monkeypatch.setattr(owned_launch, "current", lambda: "L1")
    monkeypatch.setattr(agent_store, "run", show)
    assert owned_launch.for_session("codex:mine") == expected


@pytest.mark.parametrize("invalid_field", ["session", "host", "native_id", "launch_id"])
def test_launch_admission_rejects_nonreciprocal_session_metadata(
    monkeypatch: pytest.MonkeyPatch, invalid_field: str
) -> None:
    session = {
        "session": "codex:mine",
        "host": "codex",
        "native_id": "mine",
        "launch_id": "L1",
    }
    session[invalid_field] = "different"

    def show(*_arguments: str) -> dict[str, object]:
        return {
            "launch": {
                "launch_id": "L1",
                "host": "codex",
                "state": "active",
                "session": "codex:mine",
            },
            "session": session,
        }

    monkeypatch.setattr(owned_launch, "current", lambda: "L1")
    monkeypatch.setattr(agent_store, "run", show)
    with pytest.raises(owned_launch.OwnedLaunchError, match="do not match"):
        owned_launch.for_session("codex:mine")
