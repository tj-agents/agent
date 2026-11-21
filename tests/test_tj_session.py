import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import agent_cli
import agent_store
import pytest
import tj_session
from codex import processes as codex_processes
from launch_contract import Liveness
from launch_errors import LaunchError
from tj_agent import worktrees
from tj_agent.host import Host


@dataclass
class Backend:
    host: Host
    native_id: str = "native-1"
    launch_id: str = "launch-1"
    inventory: object = field(default_factory=dict)
    error: Exception | None = None
    during_probe: Callable[[], None] | None = None
    calls: list[tuple[str, ...]] = field(default_factory=list)
    probes: int = 0
    current: dict[str, object] = field(init=False)
    bound: dict[str, object] = field(init=False)
    launch: dict[str, object] = field(init=False)

    def __post_init__(self) -> None:
        self.current = {
            "session": self.session,
            "native_id": self.native_id,
            "host": self.host.value,
            "launch_id": self.launch_id,
            "state": "active",
        }
        self.bound = dict(self.current)
        self.launch = {
            "session": self.session,
            "host": self.host.value,
            "launch_id": self.launch_id,
            "state": "ended",
        }

    @property
    def session(self) -> str:
        return f"{self.host}:{self.native_id}"

    def show(self, *arguments: str) -> dict[str, object]:
        self.calls.append(arguments)
        if arguments == ("show", self.session):
            return {"session": self.current}
        assert arguments == ("show", "--launch-id", self.launch_id)
        return {"session": self.bound, "launch": self.launch}

    def liveness(self, *, strict: bool = False) -> object:
        assert strict
        self.probes += 1
        if self.error:
            raise self.error
        if self.during_probe:
            self.during_probe()
        return self.inventory

    def adapter(self, host: Host) -> SimpleNamespace:
        assert host is self.host
        return SimpleNamespace(liveness=self.liveness)

    def check(self) -> int:
        return tj_session.main(
            ["liveness", "--session", self.session, "--launch-id", self.launch_id]
        )


@pytest.fixture(params=list(Host))
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Backend:
    host = request.param
    assert isinstance(host, Host)
    result = Backend(host)
    monkeypatch.setattr(agent_store, "run", result.show)
    monkeypatch.setattr(agent_cli, "host_launch", result.adapter)

    def no_ambient_owner(_environment: object) -> None:
        raise AssertionError("A read-only query must not require a calling owner")

    monkeypatch.setattr(worktrees, "current_owner", no_ambient_owner)
    monkeypatch.setenv("TJ_RUN_LAUNCH_ID", "unrelated-launch")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "unrelated-claude")
    monkeypatch.setenv("CODEX_THREAD_ID", "unrelated-codex")
    return result


@pytest.mark.parametrize(
    ("observed", "state"),
    [
        (Liveness(True, activity="active"), "live"),
        (Liveness(True, activity="idle"), "live"),
        (Liveness(False, activity="notLoaded"), "dead"),
        (None, "dead"),
    ],
)
def test_liveness_uses_strict_native_state_with_exact_binding(
    backend: Backend,
    capsys: pytest.CaptureFixture[str],
    observed: Liveness | None,
    state: str,
) -> None:
    backend.inventory = {"another-native": Liveness(True)}
    if observed is not None:
        backend.inventory[backend.native_id] = observed
    if backend.host is Host.CODEX and observed is None:
        assert backend.check() != 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "liveness is unverified" in captured.err
        return
    assert backend.check() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "session": backend.session,
        "launch_id": backend.launch_id,
        "state": state,
    }
    assert len(captured.out.splitlines()) == 1
    assert backend.probes == 1
    assert (
        backend.calls
        == [
            ("show", backend.session),
            ("show", "--launch-id", backend.launch_id),
        ]
        * 2
    )


@pytest.mark.parametrize(
    "failure",
    [
        LaunchError("native inventory unavailable"),
        OSError("provider unavailable"),
        RuntimeError("daemon disconnected"),
        subprocess.TimeoutExpired("native inventory", 30),
    ],
)
def test_unknown_native_state_never_prints_dead(
    backend: Backend, capsys: pytest.CaptureFixture[str], failure: Exception
) -> None:
    backend.error = failure
    assert backend.check() != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "tj session liveness:" in captured.err


@pytest.mark.parametrize("running", [False, True])
def test_unstarted_recovery_remains_retryable_unless_the_native_session_is_live(
    backend: Backend,
    capsys: pytest.CaptureFixture[str],
    running: bool,
) -> None:
    backend.launch["end_reason"] = "not_started"
    backend.inventory = {backend.native_id: Liveness(True)} if running else {}

    assert backend.check() == 0
    assert json.loads(capsys.readouterr().out)["state"] == (
        "live" if running else "dead"
    )


@pytest.mark.parametrize("alive", [True, False])
def test_replacement_receipt_cannot_prove_an_unobserved_predecessor_stopped(
    backend: Backend,
    capsys: pytest.CaptureFixture[str],
    alive: bool,
) -> None:
    backend.inventory = {
        backend.native_id: Liveness(
            alive, process_ids=(42,) if alive else (), launch_ids=("replacement",)
        )
    }
    if backend.host is Host.CODEX:
        assert backend.check() != 0
        captured = capsys.readouterr()
        assert not captured.out
        assert "launch has no native process evidence" in captured.err
    else:
        assert backend.check() == 0


def test_observed_predecessor_retains_competing_replacement_processes(
    backend: Backend,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend.inventory = {
        backend.native_id: Liveness(
            True,
            process_ids=(42, 43),
            launch_ids=(backend.launch_id, "replacement"),
        )
    }
    assert backend.check() == 0
    assert json.loads(capsys.readouterr().out)["process_ids"] == [42, 43]


@pytest.mark.parametrize(
    "inventory", [None, [], {"native-1": None}, {1: Liveness(False)}]
)
def test_malformed_inventory_is_not_native_death(
    backend: Backend, capsys: pytest.CaptureFixture[str], inventory: object
) -> None:
    backend.inventory = inventory
    assert backend.check() != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid liveness inventory" in captured.err


@pytest.mark.parametrize("row", ["current", "bound", "launch"])
@pytest.mark.parametrize("key", ["session", "host", "launch_id"])
def test_nonreciprocal_binding_is_rejected_before_the_native_probe(
    backend: Backend, capsys: pytest.CaptureFixture[str], row: str, key: str
) -> None:
    getattr(backend, row)[key] = "different"
    assert backend.check() != 0
    assert backend.probes == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err


@pytest.mark.parametrize("row", ["current", "bound"])
def test_wrong_native_id_cannot_authorize_dead_output(
    backend: Backend, capsys: pytest.CaptureFixture[str], row: str
) -> None:
    getattr(backend, row)["native_id"] = "another-native"
    assert backend.check() != 0
    assert backend.probes == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("row", ["current", "bound", "launch"])
def test_binding_changed_during_probe_is_not_accepted(
    backend: Backend, capsys: pytest.CaptureFixture[str], row: str
) -> None:
    def resume() -> None:
        getattr(backend, row)["launch_id"] = "resumed-launch"

    backend.during_probe = resume
    assert backend.check() != 0
    assert backend.probes == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err


@pytest.mark.parametrize("response", [{}, {"session": None}, {"session": []}])
def test_missing_store_binding_never_prints_dead(
    backend: Backend,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    response: dict[str, object],
) -> None:
    monkeypatch.setattr(agent_store, "run", lambda *_arguments: response)
    assert backend.check() != 0
    assert backend.probes == 0
    assert capsys.readouterr().out == ""


def test_store_failure_never_prints_dead(
    backend: Backend,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unavailable(*_arguments: str) -> dict[str, object]:
        raise agent_store.AgentStoreError("unavailable", "store unavailable")

    monkeypatch.setattr(agent_store, "run", unavailable)
    assert backend.check() != 0
    assert backend.probes == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "store unavailable" in captured.err


@pytest.mark.parametrize(
    "session", ["", "codex", "other:one", "codex:", "codex:a:b", "codex: a"]
)
def test_invalid_handle_never_queries_or_prints_native_state(
    backend: Backend, capsys: pytest.CaptureFixture[str], session: str
) -> None:
    assert (
        tj_session.main(
            ["liveness", "--session", session, "--launch-id", backend.launch_id]
        )
        != 0
    )
    assert backend.calls == []
    assert backend.probes == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("owned", [False, True])
def test_claim_worktree_keeps_its_existing_owner_and_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    owned: bool,
) -> None:
    owner = worktrees.Owner(Host.CODEX, "current", "launch") if owned else None
    claimed: list[Path] = []
    environment: dict[str, str] = {"TJ_RUN_LAUNCH_ID": "current-launch"}
    monkeypatch.setattr(os, "environ", environment)

    def current(received_environment: object) -> worktrees.Owner | None:
        assert received_environment is environment
        return owner

    def claim(path: Path, received: worktrees.Owner) -> worktrees.Ownership:
        assert received is owner
        claimed.append(path)
        return worktrees.Ownership(str(path), received, "fixture-time")

    monkeypatch.setattr(worktrees, "current_owner", current)
    monkeypatch.setattr(worktrees, "claim", claim)
    assert tj_session.main(["claim-worktree", "--path", str(tmp_path)]) == 0
    assert claimed == ([tmp_path] if owned else [])
    assert json.loads(capsys.readouterr().out) == {
        "ownership": worktrees.Ownership(
            str(tmp_path), owner, "fixture-time"
        ).document()
        if owner is not None
        else None
    }


@dataclass
class LoadedThreads:
    native_id: str
    loaded: bool
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def __enter__(self) -> "LoadedThreads":
        return self

    def __exit__(self, *_arguments: object) -> None:
        pass

    def request(self, method: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, arguments))
        if method == "thread/loaded/list":
            if arguments["cursor"] is None:
                return {"data": ["another-thread"], "nextCursor": "last-page"}
            assert arguments["cursor"] == "last-page"
            return {
                "data": [self.native_id] if self.loaded else [],
                "nextCursor": None,
            }
        assert method == "thread/read"
        return {"thread": {"status": {"type": "idle"}}}


@pytest.mark.parametrize(
    ("loaded", "recorded"), [(False, False), (False, True), (True, False)]
)
def test_codex_strict_inventory_checks_every_page_for_a_loaded_conversation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    loaded: bool,
    recorded: bool,
) -> None:
    backend = Backend(Host.CODEX)
    adapter = agent_cli.host_launch(Host.CODEX)
    server = LoadedThreads(backend.native_id, loaded)

    def version(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        assert command == ["fixture-codex", "app-server", "daemon", "version"]
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(agent_store, "run", backend.show)

    def executable(*, match_daemon: bool) -> str:
        assert match_daemon
        return "fixture-codex"

    monkeypatch.setattr(adapter, "executable", executable)
    monkeypatch.setattr(adapter, "daemon", lambda: server)
    monkeypatch.setattr(
        codex_processes,
        "inventory",
        lambda: {backend.native_id: Liveness(False)} if recorded else {},
    )
    # The adapter's sole process query and all daemon requests are fixture replies.
    monkeypatch.setattr("tj_cli.process.run", version)
    if loaded or recorded:
        assert backend.check() == 0
        assert json.loads(capsys.readouterr().out)["state"] == (
            "live" if loaded else "dead"
        )
    else:
        assert backend.check() != 0
        captured = capsys.readouterr()
        assert not captured.out
        assert "liveness is unverified" in captured.err
    assert server.calls[:2] == [
        ("thread/loaded/list", {"cursor": None}),
        ("thread/loaded/list", {"cursor": "last-page"}),
    ]
    assert all(
        method in {"thread/loaded/list", "thread/read"} for method, _ in server.calls
    )
