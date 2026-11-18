"""Normalize Codex's native turn identity and final-response evidence."""

from collections.abc import Mapping

import completion_event


def rejected_prompt(reason: str) -> dict[str, str]:
    return {"decision": "block", "reason": reason}


def parse(payload: Mapping[str, object]) -> completion_event.Event | None:
    if payload.get("agent_id"):
        return None
    session = f"codex:{completion_event.text(payload, 'session_id')}"
    turn = completion_event.text(payload, "turn_id")
    match payload.get("hook_event_name"):
        case "UserPromptSubmit":
            return completion_event.Event(session, turn, completion_event.Kind.START)
        case "Stop":
            return completion_event.Event(
                session,
                turn,
                completion_event.Kind.END,
                completion_event.text(payload, "last_assistant_message"),
            )
        case "Interrupt":
            return completion_event.Event(
                session,
                turn,
                completion_event.Kind.INTERRUPT,
                pending="The user interrupted the Codex turn",
            )
        case _:
            raise completion_event.EventError("Unsupported Codex completion event")
