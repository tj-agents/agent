"""Normalize Claude's prompt identity and native completion evidence."""

from collections.abc import Mapping

import completion_event


def rejected_prompt(reason: str) -> dict[str, str]:
    return {"decision": "block", "reason": reason}


def parse(payload: Mapping[str, object]) -> completion_event.Event | None:
    if payload.get("agent_id"):
        return None
    session = f"claude:{completion_event.text(payload, 'session_id')}"
    turn = completion_event.text(payload, "prompt_id")
    match payload.get("hook_event_name"):
        case "UserPromptSubmit":
            return completion_event.Event(session, turn, completion_event.Kind.START)
        case "Stop":
            tasks = payload.get("background_tasks")
            crons = payload.get("session_crons")
            if not isinstance(tasks, list) or not isinstance(crons, list):
                pending = "Claude's background task registry is unavailable"
            elif tasks or crons:
                pending = "Claude still has background tasks or scheduled wakeups"
            else:
                pending = None
            return completion_event.Event(
                session,
                turn,
                completion_event.Kind.END,
                completion_event.text(payload, "last_assistant_message"),
                pending,
            )
        case "StopFailure":
            return completion_event.Event(
                session,
                turn,
                completion_event.Kind.INTERRUPT,
                pending=f"Claude failed: {completion_event.text(payload, 'error')}",
            )
        case _:
            raise completion_event.EventError("Unsupported Claude completion event")
