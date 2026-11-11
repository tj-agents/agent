from __future__ import annotations

import io
import json
import subprocess
import sys
from unittest import mock

import pytest
import session_hook
from claude import idle


@pytest.mark.parametrize(
    ("tasks", "wakeups", "expected"),
    [
        ([], [], "0"),
        ([{"id": "shell"}], [], "1"),
        ([], [{"id": "timer"}], "1"),
        (None, [], "unknown"),
        ([], None, "unknown"),
        (["bad"], [], "unknown"),
    ],
)
def test_native_background_work_is_never_assumed_finished(
    tasks: object, wakeups: object, expected: str
) -> None:
    assert (
        idle.background({"background_tasks": tasks, "session_crons": wakeups})
        == expected
    )


@pytest.mark.parametrize(
    ("data", "arguments"),
    [
        (
            {"hook_event_name": "Stop", "background_tasks": [], "session_crons": []},
            ["idle", "--background", "0"],
        ),
        ({"hook_event_name": "Stop"}, ["idle", "--background", "unknown"]),
        ({"hook_event_name": "PreToolUse"}, ["done", "--cancel"]),
    ],
)
def test_wrapper_receives_native_idle_or_new_work(
    data: dict[str, object], arguments: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TJ_RUN_LAUNCH_ID", "fixture")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    with mock.patch.object(
        idle.process, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
    ) as run:
        assert session_hook.main(["idle", "--host", "claude"]) == 0
    assert run.call_args.args[0] == ["tj", *arguments]


def test_unmanaged_session_spawns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TJ_RUN_LAUNCH_ID", raising=False)
    with mock.patch.object(idle.process, "run") as run:
        assert session_hook.main(["idle", "--host", "claude"]) == 0
    run.assert_not_called()


def test_idle_signal_failure_never_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TJ_RUN_LAUNCH_ID", "fixture")
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"hook_event_name":"Stop"}'))
    with mock.patch.object(idle.process, "run", side_effect=OSError("unavailable")):
        assert session_hook.main(["idle", "--host", "claude"]) == 0
