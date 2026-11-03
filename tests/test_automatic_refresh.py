import io
import json
import shlex
import sys
from collections.abc import Iterator
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import automatic_refresh
import close_lifecycle
import managed_admission as admission
import plugin_refresh
import pytest
import required_plugins
import session_relaunch
from host import Host
from test_managed_admission import AdmissionFixture


@pytest.fixture
def case() -> Iterator[AdmissionFixture]:
    fixture = AdmissionFixture()
    try:
        yield fixture
    finally:
        fixture.stack.close()
        fixture.temporary.cleanup()


def native(generation: str = "one") -> dict[str, str]:
    return {"input_generation": generation, "turn_id": "turn-" + generation}


def prepare(case: AdmissionFixture, host: Host) -> None:
    case.verify(host)
    case.bind(host)


def test_refresh_hook_entrypoint_uses_the_native_payload() -> None:
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session",
        "cwd": "project",
    }
    verdict = {"decision": "block", "reason": "continue"}
    with (
        patch.object(sys, "argv", ["automatic_refresh.py", "--host", "codex"]),
        patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
        patch.object(automatic_refresh, "run", return_value=verdict) as run,
        redirect_stdout(io.StringIO()) as output,
    ):
        assert automatic_refresh.main() == 0
    run.assert_called_once_with(Path("project"), Host.CODEX, "session", "Stop")
    assert json.loads(output.getvalue()) == verdict


def test_relaunch_rejects_a_different_session_before_process_lookup(
    case: AdmissionFixture,
) -> None:
    shared = SimpleNamespace(
        own_host_and_entry=lambda: {"host": "claude", "session_id": "actual"}
    )

    def arguments(_: int) -> list[str]:
        raise AssertionError("relaunch read a process before validating its session")

    with (
        patch.object(session_relaunch, "claim_record") as claim,
        pytest.raises(RuntimeError, match="differs from the automatic refresh hook"),
    ):
        session_relaunch.relaunch(
            case.project,
            {},
            shared=shared,
            arguments=arguments,
            host=Host.CLAUDE,
            expected_session="expected",
        )
    claim.assert_not_called()


@pytest.mark.parametrize("host", list(Host))
def test_healthy_sessions_do_not_attempt_automatic_maintenance(
    case: AdmissionFixture, host: Host
) -> None:
    with (
        patch.object(admission, "gate") as gate,
        patch.object(automatic_refresh, "maintain") as maintain,
        patch.object(
            required_plugins,
            "inventory",
            side_effect=AssertionError(
                "automatic healthy path queried native inventory"
            ),
        ),
        patch.object(
            plugin_refresh,
            "package_tree",
            side_effect=AssertionError("automatic healthy path hashed a package"),
        ),
    ):
        assert (
            automatic_refresh.run(case.project, host, "session", "UserPromptSubmit")
            == {}
        )
    gate.assert_called_once()
    maintain.assert_not_called()


@pytest.mark.parametrize("host", list(Host))
def test_same_content_renewal_does_not_restart_after_expiry(
    case: AdmissionFixture, host: Host
) -> None:
    prepare(case, host)
    with (
        patch.object(
            admission,
            "gate",
            side_effect=admission.AdmissionError("Managed admission expired"),
        ),
        patch.object(
            admission,
            "cached",
            side_effect=admission.AdmissionError("Managed admission expired"),
        ),
        patch.object(automatic_refresh, "maintain") as maintain,
        patch.object(automatic_refresh, "unchanged", return_value=True),
        patch.object(close_lifecycle, "current_input", return_value=native()),
        patch.object(close_lifecycle, "require_input") as require_input,
        patch.object(session_relaunch, "relaunch") as relaunch,
    ):
        assert (
            automatic_refresh.run(case.project, host, "session", "UserPromptSubmit")
            == {}
        )
    maintain.assert_called_once()
    require_input.assert_called_once_with(host, "session", native())
    relaunch.assert_not_called()
    assert (
        admission.read(automatic_refresh.state_path(case.project, host, "session"))[
            "status"
        ]
        == "renewed"
    )


@pytest.mark.parametrize("host", list(Host))
def test_changed_contents_arm_a_session_bound_restart(
    case: AdmissionFixture, host: Host
) -> None:
    prepare(case, host)
    with (
        patch.object(
            admission,
            "gate",
            side_effect=admission.AdmissionError("Managed admission expired"),
        ),
        patch.object(automatic_refresh, "maintain"),
        patch.object(automatic_refresh, "unchanged", return_value=False),
        patch.object(close_lifecycle, "current_input", return_value=native()),
        patch.object(close_lifecycle, "require_input"),
        patch.object(
            session_relaunch,
            "relaunch",
            return_value={"relaunch": "armed"},
        ) as relaunch,
    ):
        output = automatic_refresh.run(
            case.project, host, "session", "UserPromptSubmit"
        )
    assert (
        "guarded restart is armed" in output["hookSpecificOutput"]["additionalContext"]
    )
    relaunch.assert_called_once_with(
        case.project,
        {},
        host=host,
        expected_session="session",
        expected_input=native(),
    )
    assert (
        admission.read(automatic_refresh.state_path(case.project, host, "session"))[
            "status"
        ]
        == "restart-armed"
    )


def test_failed_maintenance_is_retained_until_a_new_input_retries(
    case: AdmissionFixture,
) -> None:
    prepare(case, Host.CODEX)
    failed = admission.AdmissionError("offline")
    with (
        patch.object(admission, "gate", side_effect=failed),
        patch.object(admission, "cached", side_effect=failed),
        patch.object(
            automatic_refresh, "maintain", side_effect=[failed, failed]
        ) as maintain,
        patch.object(
            close_lifecycle,
            "current_input",
            side_effect=[native(), native(), native("two")],
        ),
        patch.object(session_relaunch, "relaunch") as relaunch,
    ):
        first = automatic_refresh.run(
            case.project, Host.CODEX, "session", "UserPromptSubmit"
        )
        second = automatic_refresh.run(
            case.project, Host.CODEX, "session", "UserPromptSubmit"
        )
        third = automatic_refresh.run(
            case.project, Host.CODEX, "session", "UserPromptSubmit"
        )
    assert "remains pending" in first["hookSpecificOutput"]["additionalContext"]
    assert (
        "same input will not start another"
        in second["hookSpecificOutput"]["additionalContext"]
    )
    assert "remains pending" in third["hookSpecificOutput"]["additionalContext"]
    assert maintain.call_count == 2
    relaunch.assert_not_called()
    state = admission.read(
        automatic_refresh.state_path(case.project, Host.CODEX, "session")
    )
    assert state["status"] == "failed"


def test_second_invalidation_on_the_same_input_retries_after_a_renewal(
    case: AdmissionFixture,
) -> None:
    prepare(case, Host.CODEX)
    inputs = [native(), native()]
    identities = ["first", "renewed", "second", "renewed-again"]
    with (
        patch.object(
            admission,
            "gate",
            side_effect=admission.AdmissionError("Managed admission expired"),
        ),
        patch.object(automatic_refresh, "maintain") as maintain,
        patch.object(automatic_refresh, "unchanged", return_value=True),
        patch.object(
            close_lifecycle,
            "current_input",
            side_effect=inputs,
        ),
        patch.object(close_lifecycle, "require_input"),
        patch.object(automatic_refresh, "stale_identity", side_effect=identities),
    ):
        automatic_refresh.run(case.project, Host.CODEX, "session", "UserPromptSubmit")
        automatic_refresh.run(case.project, Host.CODEX, "session", "UserPromptSubmit")
    maintain.assert_not_called()


def test_changed_native_input_prevents_a_restart(
    case: AdmissionFixture,
) -> None:
    prepare(case, Host.CODEX)
    changed = ValueError("native input changed during the close transition")
    with (
        patch.object(
            admission,
            "gate",
            side_effect=admission.AdmissionError("Managed admission expired"),
        ),
        patch.object(automatic_refresh, "maintain"),
        patch.object(automatic_refresh, "unchanged", return_value=False),
        patch.object(close_lifecycle, "current_input", return_value=native()),
        patch.object(
            close_lifecycle,
            "require_input",
            side_effect=[None, changed],
        ),
        patch.object(session_relaunch, "relaunch") as relaunch,
    ):
        result = automatic_refresh.run(
            case.project, Host.CODEX, "session", "UserPromptSubmit"
        )
    assert "native input changed" in result["hookSpecificOutput"]["additionalContext"]
    relaunch.assert_not_called()
    assert (
        admission.read(
            automatic_refresh.state_path(case.project, Host.CODEX, "session")
        )["status"]
        == "failed"
    )


def test_replaced_admission_during_renewal_cannot_write_or_claim_a_restart(
    case: AdmissionFixture,
) -> None:
    document = case.verify(Host.CODEX)
    case.bind(Host.CODEX)
    replacement = deepcopy(document)
    replacement["freshness"]["agent@tj-agents"] = "new-publication"
    paths = [
        admission.receipt_path(case.project, Host.CODEX, "session@" + guard)
        for guard in document["guards"]
    ]
    original = [admission.read(path) for path in paths]
    with (
        patch.object(
            admission,
            "gate",
            side_effect=admission.AdmissionError("Managed admission expired"),
        ),
        patch.object(automatic_refresh, "maintain"),
        patch.object(close_lifecycle, "current_input", return_value=native()),
        patch.object(close_lifecycle, "require_input"),
        patch.object(
            admission, "cached", side_effect=[document, document, replacement]
        ),
        patch.object(session_relaunch, "relaunch") as relaunch,
    ):
        output = automatic_refresh.run(
            case.project, Host.CODEX, "session", "UserPromptSubmit"
        )
    assert "changed during renewal" in output["hookSpecificOutput"]["additionalContext"]
    relaunch.assert_not_called()
    assert [admission.read(path) for path in paths] == original
    state = admission.read(
        automatic_refresh.state_path(case.project, Host.CODEX, "session")
    )
    assert state["status"] == "failed"
    with (
        patch.object(admission, "gate") as gate,
        patch.object(automatic_refresh, "maintain") as maintain,
        patch.object(close_lifecycle, "current_input") as current_input,
    ):
        assert automatic_refresh.run(case.project, Host.CODEX, "session", "Stop") == {}
    gate.assert_called_once()
    maintain.assert_not_called()
    current_input.assert_not_called()


def test_writer_unsettled_suppresses_retries_across_new_inputs(
    case: AdmissionFixture,
) -> None:
    prepare(case, Host.CODEX)
    expired = admission.AdmissionError("Managed admission expired")
    unsettled = automatic_refresh.MaintenanceUnsettledError("writer still active")
    with (
        patch.object(admission, "gate", side_effect=expired),
        patch.object(admission, "cached", side_effect=expired),
        patch.object(automatic_refresh, "maintain", side_effect=unsettled) as maintain,
        patch.object(
            close_lifecycle,
            "current_input",
            side_effect=[native(), native("two")],
        ),
        patch.object(session_relaunch, "relaunch") as relaunch,
    ):
        first = automatic_refresh.run(
            case.project, Host.CODEX, "session", "UserPromptSubmit"
        )
        second = automatic_refresh.run(
            case.project, Host.CODEX, "session", "UserPromptSubmit"
        )
    assert "writer still active" in first["hookSpecificOutput"]["additionalContext"]
    assert "writer still active" in second["hookSpecificOutput"]["additionalContext"]
    maintain.assert_called_once()
    relaunch.assert_not_called()
    state = admission.read(
        automatic_refresh.state_path(case.project, Host.CODEX, "session")
    )
    assert state["status"] == "writer-unsettled"


@pytest.mark.parametrize("host", list(Host))
def test_pending_close_keeps_the_close_assessment_as_owner(
    case: AdmissionFixture, host: Host
) -> None:
    with (
        patch.object(admission, "gate", side_effect=admission.AdmissionError("stale")),
        patch.object(close_lifecycle, "has_pending_close", return_value=True),
        patch.object(close_lifecycle, "current_input") as current_input,
        patch.object(automatic_refresh, "maintain") as maintain,
    ):
        assert automatic_refresh.run(case.project, host, "session", "Stop") == {}
    current_input.assert_not_called()
    maintain.assert_not_called()


def test_stop_continues_once_after_renewing_a_previously_blocked_turn(
    case: AdmissionFixture,
) -> None:
    prepare(case, Host.CODEX)
    automatic_refresh.mark_blocked(case.project, Host.CODEX, "session")
    with (
        patch.object(admission, "gate", side_effect=admission.AdmissionError("stale")),
        patch.object(automatic_refresh, "maintain") as maintain,
        patch.object(automatic_refresh, "unchanged", return_value=True),
        patch.object(close_lifecycle, "current_input", return_value=native()),
        patch.object(close_lifecycle, "require_input"),
    ):
        first = automatic_refresh.run(case.project, Host.CODEX, "session", "Stop")
        second = automatic_refresh.run(case.project, Host.CODEX, "session", "Stop")
    assert first["decision"] == "block"
    assert "Continue the authorized work" in first["reason"]
    assert second == {}
    maintain.assert_not_called()


@pytest.mark.parametrize("host", list(Host))
def test_each_host_dispatches_refresh_after_prompt_bookkeeping_and_before_stop_close(
    host: Host,
) -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = admission.read(root / f".{host.value}/hooks/agent.json")
    prompt = manifest["hooks"]["UserPromptSubmit"][1]["hooks"][0]
    stop = manifest["hooks"]["Stop"][0]["hooks"][0]
    if host is Host.CODEX:
        prompt_values = shlex.split(prompt["command"])
        stop_values = shlex.split(stop["command"])
    else:
        prompt_values = prompt["args"]
        stop_values = stop["args"]
    assert prompt["timeout"] == 300
    assert stop["timeout"] == 300
    assert "290" in prompt_values
    assert "290" in stop_values
    prompt_scripts = [value for value in prompt_values if value.endswith(".py")]
    stop_scripts = [value for value in stop_values if value.endswith(".py")]
    root_name = "PLUGIN_ROOT" if host is Host.CODEX else "CLAUDE_PLUGIN_ROOT"
    assert prompt_scripts[-3:] == [
        f"${{{root_name}}}/.agents/agent-store/runtime/session_hook.py",
        f"${{{root_name}}}/.agents/agent/runtime/close_lifecycle.py",
        f"${{{root_name}}}/.agents/agent/runtime/automatic_refresh.py",
    ]
    assert stop_scripts[-3:] == [
        f"${{{root_name}}}/.agents/agent-store/runtime/session_hook.py",
        f"${{{root_name}}}/.agents/agent/runtime/automatic_refresh.py",
        f"${{{root_name}}}/.agents/agent/runtime/close_lifecycle.py",
    ]
    refresh = next(
        index
        for index, value in enumerate(prompt_values)
        if value.endswith("automatic_refresh.py")
    )
    assert prompt_values[refresh + 1 : refresh + 3] == ["--host", host.value]
    refresh = next(
        index
        for index, value in enumerate(stop_values)
        if value.endswith("automatic_refresh.py")
    )
    assert stop_values[refresh + 1 : refresh + 3] == ["--host", host.value]
