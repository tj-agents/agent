import json
import sys
from datetime import UTC, datetime

import boot_history
import lifecycle
import pytest


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_previous_boot_uses_native_history_not_current_uptime(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    commands = []
    timestamp = datetime(2026, 11, 8, 12, 34, 56, tzinfo=UTC)
    output = (
        json.dumps(["2026-11-09T15:00:00Z", timestamp.isoformat()])
        if platform == "win32"
        else " -1 abc Sun 2026-11-08 12:34:56 UTC — Sun 2026-11-08 23:00:00 UTC\n"
        "  0 def Mon 2026-11-09 15:00:00 UTC — Mon 2026-11-09 16:00:00 UTC\n"
    )

    def command(arguments: list[str]) -> str:
        commands.append(arguments)
        return output

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(lifecycle, "command", command)
    assert boot_history.previous_boot() == timestamp.timestamp()
    assert commands[0][0] == ("powershell.exe" if platform == "win32" else "journalctl")


@pytest.mark.parametrize(
    "platform,output", [("win32", "[]"), ("linux", " 0 id current")]
)
def test_previous_boot_unavailable_reports_explicit_older_option(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    output: str,
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(lifecycle, "command", lambda arguments: output)
    with pytest.raises(lifecycle.LifecycleError, match="--older"):
        boot_history.previous_boot()
