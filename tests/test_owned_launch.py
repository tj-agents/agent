import subprocess
from collections.abc import Callable

import owned_launch
import pytest
from tj_cli import process

CURRENT = owned_launch.current

type Run = Callable[..., subprocess.CompletedProcess[str]]


def missing_tj(*_arguments: object, **_options: object) -> None:
    raise FileNotFoundError("tj")


def answered(returncode: int, stdout: str) -> Run:
    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        assert command == ["tj", "launch"]
        return subprocess.CompletedProcess(command, returncode, stdout, "")

    return run


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        pytest.param(answered(0, "launch-1\n"), "launch-1", id="owned"),
        pytest.param(answered(0, ""), None, id="not-owned"),
        pytest.param(answered(2, "launch-1\n"), None, id="failed"),
        pytest.param(missing_tj, None, id="tj-missing"),
    ],
)
def test_owned_launch_is_only_what_tj_launch_prints(
    monkeypatch: pytest.MonkeyPatch,
    run: Callable[..., object],
    expected: str | None,
) -> None:
    monkeypatch.setattr(process, "run", run)
    assert CURRENT() == expected
