import json
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import agent_cli
import owned_launch
import pytest
import session_hook
import session_resume
from codex import launch as codex_launch
from codex import processes
from launch_contract import Liveness
from launch_errors import LaunchError
from tj_agent import worktrees
from tj_agent.host import Host

SESSION = "123e4567-e89b-12d3-a456-426614174000"
STARTED = "2026-11-18T01:00:00.123456700Z"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))


def observe(monkeypatch: pytest.MonkeyPatch, pid: int, launch: str) -> None:
    def owner(arguments: list[str], value: object = None) -> dict[str, object]:
        assert arguments == ["owner", "--launch-id", launch]
        assert value is None
        return {"pid": pid, "started_at": STARTED}

    monkeypatch.setattr(processes, "query", owner)
    processes.record(SESSION, launch)


def test_inventory_preserves_competing_native_processes_and_explicit_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observe(monkeypatch, 11, "old")
    observe(monkeypatch, 22, "new")
    alive = {11, 22}

    def inspect(arguments: list[str], value: object = None) -> list[dict[str, object]]:
        assert arguments == ["inspect"]
        assert isinstance(value, list)
        return [dict(identity, alive=identity["pid"] in alive) for identity in value]

    monkeypatch.setattr(processes, "query", inspect)
    assert processes.inventory()[SESSION].process_ids == (11, 22)
    assert processes.inventory()[SESSION].launch_ids == ("new", "old")
    alive.remove(11)
    assert processes.inventory()[SESSION].process_ids == (22,)
    alive.clear()
    stopped = processes.inventory()[SESSION]
    assert not stopped.alive
    assert stopped.process_ids == ()
    assert stopped.launch_ids == ("new", "old")


def test_unobservable_process_is_not_reported_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observe(monkeypatch, 11, "old")

    def denied(arguments: list[str], value: object = None) -> object:
        raise LaunchError("Access denied while inspecting native process")

    monkeypatch.setattr(processes, "query", denied)
    with pytest.raises(LaunchError, match="Access denied"):
        processes.inventory()


def test_launch_confirmation_does_not_use_another_wrappers_live_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observe(monkeypatch, 11, "predecessor")
    observe(monkeypatch, 22, "replacement")

    def inspect(arguments: list[str], value: object = None) -> list[dict[str, object]]:
        assert arguments == ["inspect"]
        assert isinstance(value, list)
        return [dict(identity, alive=identity["pid"] == 11) for identity in value]

    monkeypatch.setattr(processes, "query", inspect)
    assert processes.inventory()[SESSION].alive
    assert not processes.inventory(launch_id="replacement")[SESSION].alive
    assert processes.inventory(launch_id="unobserved") == {}


def test_prepared_native_child_is_observed_before_a_session_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def spawned(arguments: list[str], value: object = None) -> dict[str, object]:
        assert arguments == [
            "spawned",
            "--launch-id",
            "replacement",
            "--session",
            f"codex:{SESSION}",
        ]
        assert value is None
        return {
            "launch_id": "replacement",
            "session": f"codex:{SESSION}",
            "native": {"pid": 22, "started_at": STARTED},
        }

    monkeypatch.setattr(processes, "query", spawned)
    processes.record_prepared(SESSION, "replacement")
    paths = list(processes.state_directory().glob("*.json"))
    assert len(paths) == 1
    assert processes.Observation.parse(
        json.loads(paths[0].read_text(encoding="utf-8"))
    ) == processes.Observation(SESSION, "replacement", processes.Identity(22, STARTED))


@pytest.mark.parametrize("mismatch", ["launch_id", "session", "native"])
def test_prepared_observation_rejects_a_mismatched_spawn_receipt(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    receipt: dict[str, object] = {
        "launch_id": "replacement",
        "session": f"codex:{SESSION}",
        "native": {"pid": 22, "started_at": STARTED},
    }
    receipt[mismatch] = "different"
    monkeypatch.setattr(processes, "query", lambda *_arguments: receipt)
    with pytest.raises(ValueError, match=r"different|must be an object"):
        processes.record_prepared(SESSION, "replacement")
    assert not processes.state_directory().exists()


@pytest.mark.parametrize(
    "response",
    [
        [],
        [{"pid": 12, "started_at": STARTED, "alive": False}],
        [{"pid": 11, "started_at": "2026-11-18T01:01:00Z", "alive": False}],
        [{"pid": 11, "started_at": STARTED, "alive": 0}],
    ],
)
def test_missing_or_mismatched_inspection_is_not_death(
    monkeypatch: pytest.MonkeyPatch, response: object
) -> None:
    observe(monkeypatch, 11, "old")
    monkeypatch.setattr(processes, "query", lambda *_arguments: response)
    with pytest.raises(ValueError, match="evidence"):
        processes.inventory()


def test_receipts_are_idempotent_and_scoped_to_the_native_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observe(monkeypatch, 11, "old")
    observe(monkeypatch, 11, "old")
    assert len(list(processes.state_directory().glob("*.json"))) == 1
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "different-profile"))
    assert processes.inventory() == {}


def test_corrupt_evidence_does_not_disappear_from_the_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observe(monkeypatch, 11, "old")
    path = next(processes.state_directory().glob("*.json"))
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError, match="property name"):
        processes.inventory()


def test_failed_process_proof_publishes_no_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(arguments: list[str], value: object = None) -> object:
        raise LaunchError("Wrong wrapper")

    monkeypatch.setattr(processes, "query", refused)
    with pytest.raises(LaunchError, match="Wrong wrapper"):
        processes.record(SESSION, "foreign")
    assert not processes.state_directory().exists()


def test_failed_native_process_proof_precedes_registration_and_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(session_hook, "worktree", lambda _cwd: None)
    monkeypatch.setattr(codex_launch, "registered_session", lambda _session: None)
    monkeypatch.setattr(owned_launch, "for_session", lambda _session: "owned")
    monkeypatch.setattr(
        session_hook, "register", lambda *args: events.append("register")
    )

    def refused(_session: str, _launch: str) -> None:
        raise LaunchError("Native identity unavailable")

    monkeypatch.setattr(processes, "record", refused)
    with pytest.raises(LaunchError, match="Native identity unavailable"):
        session_hook.start(
            Host.CODEX, SESSION, "/fixture", {"TJ_RUN_LAUNCH_ID": "owned"}
        )
    assert not events


@pytest.mark.parametrize("registered", [False, True])
def test_every_owned_start_records_the_current_process_before_binding(
    monkeypatch: pytest.MonkeyPatch,
    registered: bool,
) -> None:
    events: list[object] = []
    session = {"session": f"codex:{SESSION}", "launch_id": "owned"}
    monkeypatch.setattr(session_hook, "worktree", lambda _cwd: None)
    monkeypatch.setattr(
        codex_launch, "registered_session", lambda _: session if registered else None
    )
    monkeypatch.setattr(owned_launch, "for_session", lambda _: "owned")
    monkeypatch.setattr(processes, "record", lambda *args: events.append(args))

    def bind(*args: object) -> None:
        events.append("binding")

    def register(*args: object) -> dict[str, str]:
        events.append("register")
        return session

    monkeypatch.setattr(
        session_resume,
        "bind_started",
        bind,
    )
    monkeypatch.setattr(session_hook, "register", register)
    monkeypatch.setattr(
        agent_cli,
        "host_launch",
        lambda _: SimpleNamespace(
            record_session_environment=lambda *args, **kwargs: None
        ),
    )
    assert (
        session_hook.start(
            Host.CODEX, SESSION, "/fixture", {"TJ_RUN_LAUNCH_ID": "owned"}
        )
        == session
    )
    assert events[:2] == [(SESSION, "owned"), "binding"]


def test_startup_claims_precede_optional_native_naming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    session = {"launch_id": "owned", "title": "Task"}
    monkeypatch.setattr(session_hook, "worktree", lambda _: "/fixture")
    monkeypatch.setattr(codex_launch, "registered_session", lambda _: session)
    monkeypatch.setattr(
        worktrees,
        "claim_started_worktree",
        lambda *args: events.append("claim"),
    )

    def name(*args: object) -> None:
        events.append("name")
        raise LaunchError("Name service unavailable")

    monkeypatch.setattr(
        agent_cli,
        "host_launch",
        lambda _: SimpleNamespace(
            record_session_environment=lambda *args, **kwargs: None,
            name_session=name,
        ),
    )
    with pytest.raises(LaunchError, match="Name service unavailable"):
        session_hook.start(Host.CODEX, SESSION, "/fixture", {})
    assert events == ["claim", "name"]


def test_codex_start_hook_covers_its_bounded_ownership_operations() -> None:
    root = Path(__file__).resolve().parents[1]
    hooks = json.loads((root / ".codex/hooks/agent.json").read_text(encoding="utf-8"))
    # Git root, existing binding, owning launch, process proof, resume and claims.
    ownership_budget = 5 + 40 + 40 + 20 + 110 + 120
    assert hooks["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] > ownership_budget


def test_an_absent_daemon_does_not_erase_an_embedded_native_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {SESSION: Liveness(True, process_ids=(11,))}
    monkeypatch.setattr(processes, "inventory", lambda: expected)
    monkeypatch.setattr(codex_launch, "executable", lambda: "codex")
    monkeypatch.setattr(
        codex_launch.process,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 1, "", "failed to connect to native control socket (os error 2)"
        ),
    )
    assert codex_launch.liveness(strict=True) == expected


@pytest.mark.parametrize("status", ["idle", "notLoaded"])
def test_daemon_state_never_erases_an_embedded_owner(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    def request(method: str, arguments: object = None) -> dict[str, object]:
        if method == "thread/loaded/list":
            return {"data": [SESSION], "nextCursor": None}
        assert method == "thread/read"
        return {"thread": {"status": {"type": status}}}

    monkeypatch.setattr(
        processes, "inventory", lambda: {SESSION: Liveness(True, process_ids=(11,))}
    )
    server = SimpleNamespace(request=request)
    monkeypatch.setattr(codex_launch, "daemon", lambda: nullcontext(server))
    live = codex_launch.liveness()[SESSION]
    assert live.alive
    assert live.process_ids == (11,)
    assert live.activity == ("idle" if status == "idle" else None)


def test_status_keeps_verified_processes_when_daemon_inspection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {SESSION: Liveness(True, process_ids=(11,))}
    monkeypatch.setattr(processes, "inventory", lambda: expected)

    def unavailable() -> None:
        raise LaunchError("daemon unavailable")

    monkeypatch.setattr(codex_launch, "daemon", unavailable)
    assert codex_launch.liveness() == expected
