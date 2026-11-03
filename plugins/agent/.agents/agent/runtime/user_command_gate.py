"""Stop gate: block a reply handing Tommy a `!` command cli's builder did not issue."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

VERIFY_TIMEOUT_SECONDS = 20
SHOWN_CHARACTERS = 200


def builder() -> Path:
    state = os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state"
    return Path(state) / "bin" / "cli" / "user_command.py"


def reply(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    message = data.get("last_assistant_message") or data.get("lastAssistantMessage")
    return message if isinstance(message, str) else ""


def unissued(message: str, path: Path) -> list[str] | None:
    result = subprocess.run(
        [sys.executable, "-B", str(path), "verify"],
        input=message.encode("utf-8"),
        capture_output=True,
        timeout=VERIFY_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode not in (0, 1):
        error = result.stderr.decode("utf-8", "replace").strip()
        print(f"user_command_gate: verify failed: {error}", file=sys.stderr)
        return None
    if result.returncode == 0:
        return []
    return result.stdout.decode("utf-8", "replace").splitlines()


def shown(command: str) -> str:
    if len(command) <= SHOWN_CHARACTERS:
        return command
    return command[:SHOWN_CHARACTERS] + "…"


def reason(message: str) -> str | None:
    path = builder()
    if not path.is_file():
        return (
            "This reply hands Tommy a `!` command, but cli's command builder is not "
            f"installed at {path}. Run agent:refresh-plugins and build the command "
            "with it, or remove the command."
        )
    try:
        commands = unissued(message, path)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"user_command_gate: verify could not run: {error}", file=sys.stderr)
        return None
    if not commands:
        return None
    listed = "\n".join(shown(command) for command in commands)
    return (
        "Commands Tommy runs come only from cli's builder. This reply has commands "
        f"it did not issue:\n{listed}\nBuild each with cli:clip's `user_command.py "
        "issue --spec <file>` and paste its printed line verbatim, or remove it."
    )


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    message = reply(data)
    if "!" not in message:
        return 0
    blocked = reason(message)
    if blocked:
        print(json.dumps({"decision": "block", "reason": blocked}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
