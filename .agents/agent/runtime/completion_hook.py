"""Forward native lifecycle evidence without asking a model to continue."""

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import cast

import completion_event
import owned_launch
from claude import completion as claude_completion
from codex import completion as codex_completion
from tj_agent.host import Host
from tj_cli import process

EVENTS: Mapping[Host, completion_event.Events] = {
    Host.CLAUDE: claude_completion,
    Host.CODEX: codex_completion,
}
FINISH_GUIDANCE = (
    "When the whole requested goal, validation, and any required handoff are complete, "
    "run `tj finish` before writing your final response. Continue pending work and "
    "respect any keep-open request."
)


def record(event: completion_event.Event) -> dict[str, str | None]:
    result = process.run(
        ["tj", "_lifecycle"],
        input=json.dumps(event.document()),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=2,
        check=False,
    )
    if result.returncode:
        raise completion_event.EventError(
            result.stderr.strip() or "The managed completion event was not recorded"
        )
    reply = json.loads(result.stdout)
    if not isinstance(reply, dict):
        raise completion_event.EventError("Invalid managed completion receipt")
    message = reply.get("message")
    if message is not None and not isinstance(message, str):
        raise completion_event.EventError("Invalid managed completion status message")
    return {"message": message} if "message" in reply else {}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    hosts = {host.value: host for host in Host}
    parser.add_argument("--host", choices=list(hosts), required=True)
    parser.add_argument("--hook-mode", choices=("prompt", "status"), default="status")
    args = parser.parse_args(argv)
    message = None
    receipt = {}
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise completion_event.EventError(
                "Native completion event must be an object"
            )
        if (data.get("hook_event_name") == "UserPromptSubmit") != (
            args.hook_mode == "prompt"
        ):
            raise completion_event.EventError(
                "Native hook event does not match its mode"
            )
        event = EVENTS[hosts[args.host]].parse(cast(Mapping[str, object], data))
        if event is not None:
            receipt = record(event)
            message = receipt.get("message")
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        message = f"TJ completion remains unverified: {error}"
        if args.hook_mode == "prompt":
            try:
                managed = owned_launch.current() is not None
            except owned_launch.OwnedLaunchError:
                managed = True
            if managed:
                print(json.dumps(EVENTS[hosts[args.host]].rejected_prompt(message)))
                return 0
    output: dict[str, object] = {"systemMessage": message} if message else {}
    if args.hook_mode == "prompt" and receipt:
        output["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": FINISH_GUIDANCE,
        }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
