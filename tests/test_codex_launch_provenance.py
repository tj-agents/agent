from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

import agent_cli
import agent_store
import pytest
import session_hook
from codex import launch
from launch_errors import LaunchError
from tj_agent import worktrees
from tj_agent.host import Host


@pytest.fixture(autouse=True)
def isolated_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_daemon() -> None:
        pytest.fail("Launch provenance tests must not contact the native daemon")

    monkeypatch.setattr(agent_cli, "host_launch", lambda _: launch)
    monkeypatch.setattr(launch, "daemon", unexpected_daemon)


@pytest.fixture
def bound() -> dict[str, object]:
    return {
        "launch": {
            "launch_id": "actual-launch",
            "host": "codex",
            "session": "codex:native",
        },
        "session": {
            "session": "codex:native",
            "host": "codex",
            "native_id": "native",
            "launch_id": "actual-launch",
            "state": "active",
            "title": "Actual title",
            "terminal": {"kind": "windows-terminal", "id": "actual-tab"},
            "wrapper": {"pid": 48820, "started_at": "2026-11-11T15:45:55Z"},
            "parent": "codex:parent",
            "links": [{"item": "142", "role": "contributor"}],
        },
    }


@pytest.mark.parametrize("state", ["active", "closing"])
@pytest.mark.parametrize("coordinator_launch", [None, "coordinator-launch"])
def test_start_preserves_exact_binding_and_claim_identity(
    bound: dict[str, object],
    state: str,
    coordinator_launch: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = bound["session"]
    assert isinstance(session, dict)
    session["state"] = state
    original = deepcopy(session)
    calls: list[tuple[str, ...]] = []
    names: list[tuple[str, str]] = []
    claims: list[tuple[Host, str, Path, Mapping[str, str]]] = []

    def store(*arguments: str) -> dict[str, object]:
        calls.append(arguments)
        assert arguments[0] == "show"
        return bound

    def name(native_id: str, title: str) -> None:
        names.append((native_id, title))

    def claim(
        host: Host, native_id: str, root: Path, environ: Mapping[str, str]
    ) -> None:
        claims.append((host, native_id, root, environ))

    monkeypatch.setattr(agent_store, "run", store)
    monkeypatch.setattr(launch, "name_session", name)
    monkeypatch.setattr(session_hook, "worktree", lambda _: "/worktree")
    monkeypatch.setattr(worktrees, "claim_started_worktree", claim)
    environ = {
        "CODEX_THREAD_ID": "native",
        "CLAUDE_CODE_SESSION_ID": "coordinator",
        "WT_SESSION": "coordinator-tab",
        "AGENT_CLI_TAB_TITLE": "Coordinator title",
        "TJ_RUN_PID": "1111",
        "TJ_RUN_STARTED_AT": "2026-11-11T00:00:00Z",
    }
    if coordinator_launch is not None:
        environ["TJ_RUN_LAUNCH_ID"] = coordinator_launch

    for _ in range(2):
        result = session_hook.start(Host.CODEX, "native", "/worktree", environ)
        assert result == original

    assert session == original
    assert (
        calls
        == [
            ("show", "codex:native"),
            ("show", "--launch-id", "actual-launch"),
        ]
        * 2
    )
    assert names == [("native", "Actual title")] * 2
    assert (
        claims
        == [
            (
                Host.CODEX,
                "native",
                Path("/worktree"),
                {"CODEX_THREAD_ID": "native", "TJ_RUN_LAUNCH_ID": "actual-launch"},
            )
        ]
        * 2
    )


@pytest.mark.parametrize("code", ["not-found", "unexpected"])
def test_registered_session_distinguishes_absence_from_store_failure(
    code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def store(*arguments: str) -> dict[str, object]:
        assert arguments == ("show", "codex:native")
        raise agent_store.AgentStoreError(code, "lookup failed")

    monkeypatch.setattr(agent_store, "run", store)
    if code == "not-found":
        assert launch.registered_session("native") is None
    else:
        with pytest.raises(agent_store.AgentStoreError, match="lookup failed"):
            launch.registered_session("native")


@pytest.mark.parametrize(
    ("record", "field", "value"),
    [
        ("launch", "launch_id", "different"),
        ("launch", "host", "claude"),
        ("launch", "session", "codex:other"),
        ("session", "launch_id", "different"),
        ("session", "host", "claude"),
        ("session", "session", "codex:other"),
        ("session", "native_id", "other"),
    ],
)
def test_registered_session_refuses_inconsistent_binding(
    bound: dict[str, object],
    record: str,
    field: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inconsistent = deepcopy(bound)
    target = inconsistent[record]
    assert isinstance(target, dict)
    target[field] = value
    calls: list[tuple[str, ...]] = []

    def store(*arguments: str) -> dict[str, object]:
        calls.append(arguments)
        assert arguments[0] == "show"
        return bound if len(calls) == 1 else inconsistent

    monkeypatch.setattr(agent_store, "run", store)
    monkeypatch.setattr(session_hook, "worktree", lambda _: None)
    with pytest.raises(LaunchError, match="bindings do not match"):
        session_hook.start(Host.CODEX, "native", "/worktree", {})
    assert len(calls) == 2


@pytest.mark.parametrize(
    "session",
    [None, {"launch_id": ""}, {"launch_id": 7}, {"launch_id": "actual-launch"}],
)
def test_registered_session_refuses_malformed_session(
    session: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def store(*arguments: str) -> dict[str, object]:
        assert arguments == ("show", "codex:native")
        return {"session": session}

    monkeypatch.setattr(agent_store, "run", store)
    with pytest.raises(LaunchError, match="invalid"):
        launch.registered_session("native")


@pytest.mark.parametrize("state", ["unbound", "ended"])
def test_registered_session_does_not_claim_first_registration_or_resume(
    bound: dict[str, object], state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = bound["session"]
    assert isinstance(session, dict)
    if state == "unbound":
        session["launch_id"] = None
    else:
        session["state"] = state

    def store(*arguments: str) -> dict[str, object]:
        assert arguments == ("show", "codex:native")
        return bound

    monkeypatch.setattr(agent_store, "run", store)
    assert launch.registered_session("native") is None
