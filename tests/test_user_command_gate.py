from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / ".agents/agent/runtime/user_command_gate.py"
DISPATCH = ROOT / ".agents/hooks/hook_dispatch.py"
ISSUED = "!gh auth status"
FAKE_BUILDER = (
    "import sys\n"
    "assert sys.argv[1:] == ['verify']\n"
    "reply = sys.stdin.buffer.read().decode('utf-8')\n"
    "if 'crash' in reply:\n"
    "    sys.exit(2)\n"
    "lines = [line.strip('` ') for line in reply.splitlines()]\n"
    "found = [line for line in lines if line.startswith('!')]\n"
    f"missing = [command for command in found if command != {ISSUED!r}]\n"
    "print('\\n'.join(missing))\n"
    "sys.exit(1 if missing else 0)\n"
)


def stop(
    state: Path, message: str | None, *, dispatch: bool = False
) -> subprocess.CompletedProcess[str]:
    data = {"hook_event_name": "Stop", "session_id": "s", "stop_hook_active": False}
    if message is not None:
        data["last_assistant_message"] = message
    command = [sys.executable, "-B", str(GATE)]
    if dispatch:
        command = [sys.executable, "-B", str(DISPATCH), "--hook", str(GATE)]
    return subprocess.run(
        command,
        input=json.dumps(data),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "AGENT_STATE_DIRECTORY": str(state)},
        timeout=60,
        check=False,
    )


@pytest.fixture
def state(tmp_path: Path) -> Path:
    builder = tmp_path / "bin" / "cli" / "user_command.py"
    builder.parent.mkdir(parents=True)
    builder.write_text(FAKE_BUILDER, encoding="utf-8")
    return tmp_path


def decision(result: subprocess.CompletedProcess[str]) -> dict[str, object] | None:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


@pytest.mark.parametrize(
    "message", [None, "Done.", "Done!", f"Approve with:\n\n`{ISSUED}`"]
)
def test_replies_without_unissued_commands_pass(
    state: Path, message: str | None
) -> None:
    assert decision(stop(state, message)) is None


def test_an_unissued_command_blocks_with_the_builder_instruction(state: Path) -> None:
    blocked = decision(stop(state, f"Run:\n`{ISSUED}`\n`!gh auth login`"))
    assert blocked is not None
    assert blocked["decision"] == "block"
    reason = str(blocked["reason"])
    assert "!gh auth login" in reason
    assert ISSUED not in reason
    assert "user_command.py issue --spec" in reason


def test_the_dispatcher_carries_the_block(state: Path) -> None:
    blocked = decision(stop(state, "`!gh auth login`", dispatch=True))
    assert blocked is not None
    assert blocked["decision"] == "block"
    assert "!gh auth login" in str(blocked["reason"])


def test_a_missing_builder_blocks_until_refreshed(tmp_path: Path) -> None:
    blocked = decision(stop(tmp_path, "`!gh auth login`"))
    assert blocked is not None
    assert "agent:refresh-plugins" in str(blocked["reason"])


def test_a_failing_builder_is_reported_without_blocking(state: Path) -> None:
    result = stop(state, "`!crash`")
    assert decision(result) is None
    assert "verify failed" in result.stderr
