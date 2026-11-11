from __future__ import annotations

import subprocess
from unittest import mock

import pytest
import session_hook
from claude import idle


def test_a_managed_tool_call_withdraws_the_pending_closeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TJ_RUN_LAUNCH_ID", "fixture")
    with mock.patch.object(
        idle.process, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
    ) as run:
        assert session_hook.main(["idle", "--host", "claude"]) == 0
    assert run.call_args.args[0] == ["tj", "done", "--cancel"]


def test_unmanaged_session_spawns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TJ_RUN_LAUNCH_ID", raising=False)
    with mock.patch.object(idle.process, "run") as run:
        assert session_hook.main(["idle", "--host", "claude"]) == 0
    run.assert_not_called()


def test_a_failed_withdrawal_never_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TJ_RUN_LAUNCH_ID", "fixture")
    with mock.patch.object(idle.process, "run", side_effect=OSError("unavailable")):
        assert session_hook.main(["idle", "--host", "claude"]) == 0
