import io
import json
import subprocess
import sys
from unittest.mock import Mock

import completion_event
import completion_hook
import pytest
from tj_agent.host import Host
from tj_cli import process


@pytest.fixture(params=tuple(Host))
def host(request: pytest.FixtureRequest) -> Host:
    value: Host = request.param
    return value


def native_payload(host: Host, event: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": "native-session",
        "hook_event_name": event,
        "last_assistant_message": "Final result.\nVerified with tests.",
    }
    if host is Host.CLAUDE:
        payload.update(prompt_id="prompt-1", background_tasks=[], session_crons=[])
    else:
        payload["turn_id"] = "turn-1"
    return payload


def test_native_identity_correlates_input_with_its_final_response(host: Host) -> None:
    adapter = completion_hook.EVENTS[host]
    start = adapter.parse(native_payload(host, "UserPromptSubmit"))
    end = adapter.parse(native_payload(host, "Stop"))
    assert start is not None and end is not None
    assert start.session == end.session == f"{host}:native-session"
    assert start.turn == end.turn
    assert start.kind is completion_event.Kind.START
    assert end.kind is completion_event.Kind.END
    assert end.report == "Final result.\nVerified with tests."
    assert end.pending is None


@pytest.mark.parametrize("field", ["session_id", "last_assistant_message"])
@pytest.mark.parametrize("value", [None, "", " "])
def test_incomplete_native_evidence_cannot_supply_completion(
    host: Host, field: str, value: object
) -> None:
    payload = native_payload(host, "Stop")
    payload[field] = value
    with pytest.raises(completion_event.EventError, match=f"no {field}"):
        completion_hook.EVENTS[host].parse(payload)


def test_a_native_turn_identity_is_required(host: Host) -> None:
    payload = native_payload(host, "Stop")
    payload.pop("prompt_id" if host is Host.CLAUDE else "turn_id")
    with pytest.raises(completion_event.EventError, match=r"no (prompt_id|turn_id)"):
        completion_hook.EVENTS[host].parse(payload)


@pytest.mark.parametrize("field", ["background_tasks", "session_crons"])
@pytest.mark.parametrize("value", [None, [{"id": "pending"}]])
def test_claude_background_work_or_unknown_registry_prevents_close(
    field: str, value: object
) -> None:
    payload = native_payload(Host.CLAUDE, "Stop")
    payload[field] = value
    event = completion_hook.EVENTS[Host.CLAUDE].parse(payload)
    assert event is not None
    assert event.report is not None
    assert event.pending


def test_subagent_events_never_complete_the_parent(host: Host) -> None:
    payload = native_payload(host, "Stop")
    payload["agent_id"] = "child"
    assert completion_hook.EVENTS[host].parse(payload) is None


def test_native_failure_or_interruption_is_not_a_final_result(host: Host) -> None:
    event_name = "StopFailure" if host is Host.CLAUDE else "Interrupt"
    payload = native_payload(host, event_name)
    payload["error"] = "authentication_failed"
    event = completion_hook.EVENTS[host].parse(payload)
    assert event is not None
    assert event.kind is completion_event.Kind.INTERRUPT
    assert event.report is None
    assert event.pending


def test_hook_forwards_evidence_and_only_displays_runtime_status(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = Mock(
        return_value=subprocess.CompletedProcess(
            ["tj", "_lifecycle"],
            0,
            json.dumps({"message": "Completion not requested"}),
            "",
        )
    )
    monkeypatch.setattr(process, "run", run)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps(native_payload(host, "Stop"))),
    )
    assert completion_hook.main(["--host", host.value]) == 0
    assert run.call_args.args == (["tj", "_lifecycle"],)
    expected = completion_hook.EVENTS[host].parse(native_payload(host, "Stop"))
    assert expected is not None
    assert json.loads(run.call_args.kwargs["input"]) == expected.document()
    assert json.loads(capsys.readouterr().out) == {
        "systemMessage": "Completion not requested"
    }


def test_malformed_input_reports_unverified_state_without_continuation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{"))
    assert completion_hook.main(["--host", "codex"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"systemMessage"}
    assert "remains unverified" in output["systemMessage"]
