import io
import json
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import completion_event
import completion_hook
import owned_launch
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


@pytest.mark.parametrize("managed", [True, False])
def test_finish_guidance_requires_a_managed_turn_receipt(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    managed: bool,
) -> None:
    receipt = {"message": None} if managed else {}

    def acknowledge(
        command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        assert command == ["tj", "_lifecycle"]
        assert json.loads(str(options["input"]))["event"] == "start"
        return subprocess.CompletedProcess(command, 0, json.dumps(receipt), "")

    monkeypatch.setattr(process, "run", acknowledge)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps(native_payload(host, "UserPromptSubmit"))),
    )

    assert completion_hook.main(["--host", host.value, "--hook-mode", "prompt"]) == 0
    reply = json.loads(capsys.readouterr().out)
    if managed:
        context = reply["hookSpecificOutput"]
        assert context["hookEventName"] == "UserPromptSubmit"
        assert "tj finish" in context["additionalContext"]
        assert "whole requested goal" in context["additionalContext"]
        assert "keep-open request" in context["additionalContext"]
    else:
        assert reply == {}


@pytest.mark.parametrize("managed", [True, False, None])
def test_failed_prompt_admission_blocks_only_managed_or_unverified_owners(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    managed: bool | None,
) -> None:
    def owner() -> str | None:
        if managed is None:
            raise owned_launch.OwnedLaunchError("cannot inspect wrapper")
        return "launch" if managed else None

    monkeypatch.setattr(owned_launch, "current", owner)
    monkeypatch.setattr(
        process,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], 1, "", "managed session binding is missing"
            )
        ),
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps(native_payload(host, "UserPromptSubmit"))),
    )
    assert completion_hook.main(["--host", host.value, "--hook-mode", "prompt"]) == 0
    reply = json.loads(capsys.readouterr().out)
    if managed is False:
        assert set(reply) == {"systemMessage"}
    else:
        assert reply == {
            "decision": "block",
            "reason": (
                "TJ completion remains unverified: managed session binding is missing"
            ),
        }


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        "[]",
        "{}",
        '{"hook_event_name":"UserPromptSubmit"}',
        '{"hook_event_name":"Stop"}',
    ],
)
@pytest.mark.parametrize("managed", [True, False, None])
def test_invalid_prompt_fails_closed_for_managed_or_unknown_owner(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: str,
    managed: bool | None,
) -> None:
    def owner() -> str | None:
        if managed is None:
            raise owned_launch.OwnedLaunchError("owner unavailable")
        return "launch" if managed else None

    monkeypatch.setattr(owned_launch, "current", owner)
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert completion_hook.main(["--host", host.value, "--hook-mode", "prompt"]) == 0
    reply = json.loads(capsys.readouterr().out)
    assert set(reply) == (
        {"systemMessage"} if managed is False else {"decision", "reason"}
    )
    if managed is not False:
        assert reply["decision"] == "block"


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        "[]",
        "{}",
        '{"hook_event_name":"Stop"}',
        '{"hook_event_name":"UserPromptSubmit"}',
    ],
)
def test_invalid_stop_only_reports_status(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: str,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert completion_hook.main(["--host", host.value, "--hook-mode", "status"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"systemMessage"}


@pytest.mark.parametrize("event", ["UserPromptSubmit", "Stop"])
def test_declared_hook_routes_malformed_input_without_confusing_prompt_and_stop(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    event: str,
) -> None:
    manifest = Path(__file__).resolve().parents[1] / f".{host}/hooks/agent.json"
    hooks = json.loads(manifest.read_text(encoding="utf-8"))["hooks"]
    hook = hooks[event][0]["hooks"][0]
    monkeypatch.setattr(owned_launch, "current", lambda: "managed-launch")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{"))
    arguments = (
        hook["args"][2:] if host is Host.CLAUDE else shlex.split(hook["command"])[3:]
    )
    assert completion_hook.main(arguments) == 0
    reply = json.loads(capsys.readouterr().out)
    if event == "UserPromptSubmit":
        assert reply.get("decision") == "block"
    else:
        assert set(reply) == {"systemMessage"}
