"""Forward native lifecycle evidence without asking a model to continue."""

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import cast

import completion_event
from claude import completion as claude_completion
from codex import completion as codex_completion
from tj_agent.host import Host
from tj_cli import process

EVENTS: Mapping[Host, completion_event.Events] = {
    Host.CLAUDE: claude_completion,
    Host.CODEX: codex_completion,
}


def record(event: completion_event.Event) -> str | None:
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
    return message


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    hosts = {host.value: host for host in Host}
    parser.add_argument("--host", choices=list(hosts), required=True)
    args = parser.parse_args(argv)
    message = None
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise completion_event.EventError(
                "Native completion event must be an object"
            )
        event = EVENTS[hosts[args.host]].parse(cast(Mapping[str, object], data))
        if event is not None:
            message = record(event)
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        message = f"TJ completion remains unverified: {error}"
    # A Stop block or additionalContext can submit another model turn. Only a
    # user-visible status message is allowed; failure must not synthesize input.
    print(json.dumps({"systemMessage": message} if message else {}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
