from __future__ import annotations

import io
import json
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import agent_cli
import agent_store
import pytest
from codex import launch, permissions, startup
from launch_contract import Confirmation, Liveness, PreparedLaunch
from launch_errors import LaunchError, LaunchTimeout
from tj_agent.host import Host
from tj_cli import process

THREAD = "01a1cb49-8592-7402-9fed-9f77d3de8646"


@dataclass
class Native:
    directory: Path
    events: list[object] = field(default_factory=list)
    projects: dict[str, object] = field(default_factory=dict)
    failure: str | None = None
    result_id: object = THREAD
    model: str = "fixture-model"
    effort: str = "high"
    status: str = "idle"
    loaded: list[str] = field(default_factory=list)
    features: dict[str, object] = field(default_factory=dict)

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.events.append((method, params))
        if method == self.failure:
            raise RuntimeError(f"failed {method}")
        if method == "config/read":
            return {
                "config": {"projects": self.projects, "features": self.features},
                "layers": [],
            }
        if method == "thread/start":
            return {
                "thread": {"id": self.result_id},
                "cwd": str(self.directory),
                "model": self.model,
                "reasoningEffort": self.effort,
            }
        if method in {"thread/name/set", "thread/archive"}:
            return {}
        if method == "thread/loaded/list":
            return {"data": self.loaded, "nextCursor": None}
        if method == "thread/read":
            return {
                "thread": {
                    "id": THREAD,
                    "cwd": str(self.directory),
                    "path": str(self.directory / "rollout.jsonl"),
                    "status": {"type": self.status},
                }
            }
        pytest.fail(f"Unexpected native request: {method}")

    @contextmanager
    def connect(
        self, native: str, directory: Path, *, existing: bool = False
    ) -> Iterator[Native]:
        assert native == "fixture-codex" and directory == self.directory
        self.events.append("connect_shared" if existing else "connect")
        try:
            yield self
        finally:
            self.events.append("disconnect")


@pytest.fixture(autouse=True)
def no_live_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail(
            "Startup tests must not invoke native processes, Store or admission"
        )

    monkeypatch.setattr(process, "run", forbidden)
    monkeypatch.setattr(process, "start", forbidden)
    monkeypatch.setattr(agent_store, "run", forbidden)
    monkeypatch.setattr(startup, "connection", forbidden)
    monkeypatch.setattr(startup, "admit", forbidden)
    monkeypatch.setattr(launch, "daemon", forbidden)
    monkeypatch.setattr(launch, "resolve_executable", forbidden)
    monkeypatch.setattr(startup.processes, "inventory", forbidden)
    monkeypatch.setattr(startup.processes, "record_prepared", forbidden)
    monkeypatch.setattr(launch, "liveness", forbidden)


@pytest.fixture
def native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Native:
    fake = Native(tmp_path, projects={str(tmp_path): {"trust_level": "trusted"}})
    (tmp_path / "rollout.jsonl").write_text(
        json.dumps(
            {
                "type": "turn_context",
                "payload": {
                    "model": "fixture-model",
                    "collaboration_mode": {"settings": {"reasoning_effort": "high"}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        fake.events.append(command)
        if command == ["tj", "_session", "--help"]:
            return subprocess.CompletedProcess(
                command, 0, "  --session <SESSION>\n", ""
            )
        pytest.fail(f"Unexpected native process: {command}")

    def inventory(*, launch_id: str) -> dict[str, Liveness]:
        if fake.failure == "process/inspect":
            raise RuntimeError("Native process inspection failed")
        return {
            THREAD: Liveness(fake.status in {"idle", "active"}, launch_ids=(launch_id,))
        }

    monkeypatch.setattr(process, "run", run)
    monkeypatch.setattr(startup, "connection", fake.connect)
    monkeypatch.setattr(startup, "admit", lambda *args: fake.events.append("admit"))
    monkeypatch.setattr(startup.processes, "inventory", inventory)
    monkeypatch.setattr(startup.processes, "record_prepared", lambda *_args: True)
    monkeypatch.setattr(
        launch,
        "liveness",
        lambda **_options: {value: Liveness(True) for value in fake.loaded},
    )
    monkeypatch.setattr(
        launch, "resolve_executable", lambda **options: ("fixture-codex", {})
    )
    return fake


def prepare(native: Native) -> PreparedLaunch:
    arguments = launch.start_arguments(
        native.directory,
        "Actual title",
        'Read "file"; $value λ',
        "fixture-model",
        "high",
    )
    return cast(
        PreparedLaunch,
        startup.prepare(
            native.directory, "Actual title", arguments, "tj", "fixture-codex", {}
        ),
    )


def test_prepare_creates_names_and_disconnects_before_native_resume(
    native: Native,
) -> None:
    prepared = prepare(native)
    assert prepared == PreparedLaunch(
        (
            "fixture-codex",
            "--cd",
            str(native.directory),
            "resume",
            THREAD,
            "--model",
            "fixture-model",
            "-c",
            "model_reasoning_effort=high",
            "--remote",
            "unix://",
            "--",
            'Read "file"; $value λ',
        ),
        f"codex:{THREAD}",
        created=True,
    )
    assert native.events == [
        ["tj", "_session", "--help"],
        "admit",
        "connect",
        ("config/read", {"cwd": str(native.directory), "includeLayers": True}),
        (
            "thread/start",
            {
                "cwd": str(native.directory),
                "historyMode": "paginated",
                "threadSource": "user",
                "ephemeral": False,
                "model": "fixture-model",
                "config": {"model_reasoning_effort": "high"},
            },
        ),
        ("thread/name/set", {"threadId": THREAD, "name": "Actual title"}),
        "disconnect",
    ]


@pytest.mark.parametrize("trust", [None, {}, {"trust_level": "untrusted"}])
def test_prepare_refuses_unknown_or_untrusted_before_creation(
    native: Native, trust: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    native.projects = {str(native.directory): trust}
    monkeypatch.setattr(
        permissions.worktrees, "repository_root", lambda _: native.directory
    )
    with pytest.raises(LaunchError, match="trust"):
        prepare(native)
    assert not any(
        isinstance(event, tuple) and event[0] == "thread/start"
        for event in native.events
    )


def test_prepare_requires_cli_support_before_admission_or_creation(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        process,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, "Usage: tj _session", ""
        ),
    )
    with pytest.raises(LaunchError, match="tj _session --session"):
        prepare(native)
    assert native.events == []


@pytest.mark.parametrize("failure", ["thread/start", "thread/name/set"])
def test_prepare_archives_only_returned_thread_on_failure(
    native: Native, failure: str
) -> None:
    native.failure = failure
    with pytest.raises(LaunchError, match=failure):
        prepare(native)
    archived = [
        event
        for event in native.events
        if isinstance(event, tuple) and event[0] == "thread/archive"
    ]
    assert archived == (
        [("thread/archive", {"threadId": THREAD})]
        if failure == "thread/name/set"
        else []
    )


@pytest.mark.parametrize("thread_id", [None, "", "unrelated", THREAD.upper()])
def test_prepare_never_archives_an_unvalidated_native_id(
    native: Native, thread_id: object
) -> None:
    native.result_id = thread_id
    with pytest.raises(LaunchError, match="canonical"):
        prepare(native)
    assert not any(
        isinstance(event, tuple) and event[0] == "thread/archive"
        for event in native.events
    )


def test_prepare_rejects_changed_model_before_resume(native: Native) -> None:
    native.model = "different-model"
    with pytest.raises(LaunchError, match="different launch settings"):
        prepare(native)
    assert ("thread/archive", {"threadId": THREAD}) in native.events
    assert not any(
        isinstance(event, tuple) and event[0] == "thread/name/set"
        for event in native.events
    )


@pytest.mark.parametrize(
    ("model", "effort"),
    [("other", "high"), ("fixture-model", "low"), ("fixture-model", None)],
)
def test_confirm_rejects_first_turn_settings_even_after_correct_thread_creation(
    native: Native,
    model: str,
    effort: str | None,
) -> None:
    prepared = prepare(native)
    (native.directory / "rollout.jsonl").write_text(
        json.dumps(
            {
                "type": "turn_context",
                "payload": {
                    "model": model,
                    "collaboration_mode": {"settings": {"reasoning_effort": effort}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    arguments = launch.start_arguments(
        native.directory, "Title", "Prompt", "fixture-model", "high"
    )
    with pytest.raises(LaunchError, match="first turn used"):
        startup.confirm(prepared, arguments, "launch")


def test_confirm_rejects_a_correct_turn_without_its_owned_native_process(
    native: Native,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare(native)
    native.status = "notLoaded"
    arguments = launch.start_arguments(
        native.directory, "Title", "Prompt", "fixture-model", "high"
    )
    monkeypatch.setattr(
        agent_store,
        "run",
        lambda *args: {"launch": {"launch_id": "launch", "state": "active"}},
    )
    with pytest.raises(LaunchTimeout, match="owned native process"):
        startup.confirm(prepared, arguments, "launch", timeout=0, clock=lambda: 0)


def test_confirm_rejects_owned_frontend_without_loaded_daemon_thread(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare(native)
    native.status = "notLoaded"
    monkeypatch.setattr(
        startup.processes,
        "inventory",
        lambda **kwargs: {THREAD: Liveness(True, launch_ids=("launch",))},
    )
    arguments = launch.start_arguments(
        native.directory, "Title", "Prompt", "fixture-model", "high"
    )
    with pytest.raises(LaunchTimeout, match="attach to the managed local daemon"):
        startup.confirm(prepared, arguments, "launch", timeout=0, clock=lambda: 0)
    assert "connect_shared" in native.events


def test_confirm_waits_for_first_turn_and_ignores_partial_record(
    native: Native,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_store,
        "run",
        lambda *args: {"launch": {"launch_id": "launch", "state": "active"}},
    )
    prepared = prepare(native)
    path = native.directory / "rollout.jsonl"
    record = path.read_text(encoding="utf-8")
    path.write_text('{"type":', encoding="utf-8")
    waits: list[float] = []

    def wait(seconds: float) -> None:
        waits.append(seconds)
        path.write_text(record, encoding="utf-8")

    arguments = launch.start_arguments(
        native.directory, "Title", "Prompt", "fixture-model", "high"
    )
    startup.confirm(prepared, arguments, "launch", clock=lambda: 0, wait=wait)
    assert waits == [0.5]


def test_confirm_times_out_without_claiming_an_unstarted_turn(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        agent_store,
        "run",
        lambda *args: {"launch": {"launch_id": "launch", "state": "active"}},
    )
    prepared = prepare(native)
    (native.directory / "rollout.jsonl").unlink()
    arguments = launch.start_arguments(
        native.directory, "Title", "Prompt", "fixture-model", "high"
    )
    with pytest.raises(LaunchTimeout, match="did not record its first turn"):
        startup.confirm(prepared, arguments, "launch", timeout=0, clock=lambda: 0)


def test_confirm_reports_ended_launch_and_daemon_history_error(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare(native)
    (native.directory / "rollout.jsonl").unlink()
    monkeypatch.setattr(
        agent_store,
        "run",
        lambda *args: {
            "launch": {"launch_id": "launch", "state": "ended", "end_reason": "exited"}
        },
    )
    request = native.request

    def read(method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "thread/read" and params.get("includeTurns") is True:
            raise RuntimeError("disk I/O error")
        return request(method, params)

    monkeypatch.setattr(native, "request", read)
    arguments = launch.start_arguments(
        native.directory, "Title", "Prompt", "fixture-model", "high"
    )
    with pytest.raises(
        LaunchError, match=r"exited before its first turn.*disk I/O error"
    ):
        startup.confirm(
            prepared,
            arguments,
            "launch",
            wait=lambda _: pytest.fail("must fail immediately"),
        )


def test_confirm_accepts_turn_written_while_launch_ends(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare(native)
    path = native.directory / "rollout.jsonl"
    turn = path.read_text(encoding="utf-8")
    path.unlink()

    def show(*args: str) -> dict[str, object]:
        path.write_text(turn, encoding="utf-8")
        return {"launch": {"launch_id": "launch", "state": "ended"}}

    monkeypatch.setattr(agent_store, "run", show)
    arguments = launch.start_arguments(
        native.directory, "Title", "Prompt", "fixture-model", "high"
    )
    startup.confirm(prepared, arguments, "launch")


def test_confirm_checks_first_turn_instead_of_a_later_corrected_turn(
    native: Native,
) -> None:
    path = native.directory / "rollout.jsonl"
    path.write_text(
        '{"type":"turn_context","payload":{"model":"wrong"}}\n'
        + path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    prepared = prepare(native)
    arguments = launch.start_arguments(
        native.directory, "Title", "Prompt", "fixture-model", "high"
    )
    with pytest.raises(LaunchError, match="first turn used model='wrong'"):
        startup.confirm(prepared, arguments, "launch")


def test_launch_reports_first_turn_mismatch_and_preserves_the_running_session(
    native: Native,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (native.directory / "rollout.jsonl").write_text(
        '{"type":"turn_context","payload":{"model":"wrong"}}\n', encoding="utf-8"
    )
    monkeypatch.setattr(agent_cli, "host_launch", lambda _: launch)
    monkeypatch.setattr(shutil, "which", lambda name: name)
    monkeypatch.setattr(
        agent_cli, "open_tab", lambda *args, **kwargs: args[6](["terminal"])
    )
    monkeypatch.setattr(
        agent_cli,
        "await_launch",
        lambda launch_id, **kwargs: {
            "launch": {"state": "active"},
            "session": {"session": f"codex:{THREAD}", "launch_id": launch_id},
        },
    )
    monkeypatch.setattr(
        agent_store, "run", lambda *args: {"launch": {"state": "active"}}
    )
    arguments = launch.start_arguments(
        native.directory, "Title", "Prompt", "fixture-model", "high"
    )
    call_launch = cast(Callable[..., dict[str, object]], agent_cli.launch)
    with pytest.raises(LaunchError, match="first turn used model='wrong'"):
        call_launch(
            Host.CODEX,
            native.directory,
            "Title",
            arguments,
            run=lambda *args, **kwargs: None,
        )
    assert not any(
        isinstance(event, tuple) and event[0] == "thread/archive"
        for event in native.events
    )


def test_prepare_existing_resume_does_not_create_or_archive(native: Native) -> None:
    prepared = startup.prepare(
        native.directory,
        "Existing",
        launch.resume_arguments(native.directory, "Existing", THREAD),
        "tj",
        "fixture-codex",
        {},
    )
    startup.abandon(prepared, "new-launch", False)
    assert startup.confirm(prepared, [], "launch") == Confirmation.PROCESS_STARTED
    assert prepared.command == (
        "fixture-codex",
        "--cd",
        str(native.directory),
        "resume",
        THREAD,
        "--remote",
        "unix://",
    )
    assert prepared.session == f"codex:{THREAD}" and not prepared.created
    assert "admit" in native.events
    assert (
        "config/read",
        {"cwd": str(native.directory), "includeLayers": True},
    ) in native.events
    assert not any(
        isinstance(event, tuple)
        and event[0] in {"thread/start", "thread/name/set", "thread/archive"}
        for event in native.events
    )


def test_resume_explicitly_attaches_without_changing_daemon_auto_start_preference(
    native: Native,
) -> None:
    native.features = {"daemon_auto_start": False}
    prepared = startup.prepare(
        native.directory,
        "Existing",
        launch.resume_arguments(native.directory, "Existing", THREAD),
        "tj",
        "fixture-codex",
        {},
    )
    assert prepared.command[-2:] == ("--remote", "unix://")
    assert native.features == {"daemon_auto_start": False}


def test_resume_refuses_missing_native_history_before_dispatch(native: Native) -> None:
    native.failure = "thread/read"
    with pytest.raises(LaunchError, match="failed thread/read"):
        startup.prepare(
            native.directory,
            "Existing",
            launch.resume_arguments(native.directory, "Existing", THREAD),
            "tj",
            "fixture-codex",
            {},
        )


def test_resume_refuses_a_thread_already_loaded_before_dispatch(native: Native) -> None:
    native.loaded = [THREAD]
    with pytest.raises(LaunchError, match="already running"):
        launch.prepare(
            native.directory,
            "Existing",
            launch.resume_arguments(native.directory, "Existing", THREAD),
            "tj",
            {},
        )


def test_resume_confirmation_reports_process_errors_as_launch_failures(
    native: Native,
) -> None:
    prepared = startup.prepare(
        native.directory,
        "Existing",
        launch.resume_arguments(native.directory, "Existing", THREAD),
        "tj",
        "fixture-codex",
        {},
    )
    native.failure = "process/inspect"
    with pytest.raises(LaunchError, match="Cannot verify Codex native process"):
        startup.confirm(prepared, [], "launch")


def test_resume_requires_project_trust(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    native.projects = {}
    monkeypatch.setattr(
        permissions.worktrees, "repository_root", lambda _: native.directory
    )
    with pytest.raises(LaunchError, match="trust"):
        startup.prepare(
            native.directory,
            "Existing",
            launch.resume_arguments(native.directory, "Existing", THREAD),
            "tj",
            "fixture-codex",
            {},
        )


def test_resume_reports_only_process_start_without_a_new_turn(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = startup.prepare(
        native.directory,
        "Existing",
        launch.resume_arguments(native.directory, "Existing", THREAD),
        "tj",
        "fixture-codex",
        {},
    )
    (native.directory / "rollout.jsonl").unlink()
    native.status = "notLoaded"
    monkeypatch.setattr(
        agent_store,
        "run",
        lambda *args: {"launch": {"launch_id": "launch", "state": "active"}},
    )
    waits = []

    def wait(seconds: float) -> None:
        waits.append(seconds)
        native.status = "idle"

    assert (
        startup.confirm(prepared, [], "launch", clock=lambda: 0, wait=wait)
        == Confirmation.PROCESS_STARTED
    )
    assert waits == [0.5]


def test_resume_confirmation_rejects_a_session_without_process_evidence(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = startup.prepare(
        native.directory,
        "Existing",
        launch.resume_arguments(native.directory, "Existing", THREAD),
        "tj",
        "fixture-codex",
        {},
    )
    native.status = "notLoaded"
    monkeypatch.setattr(
        agent_store,
        "run",
        lambda *args: {"launch": {"launch_id": "launch", "state": "active"}},
    )
    with pytest.raises(LaunchTimeout, match="owned native process"):
        startup.confirm(prepared, [], "launch", timeout=0, clock=lambda: 0)


@pytest.mark.parametrize("spawned", [True, False])
def test_resume_confirmation_waits_for_the_spawned_child_without_submitting_a_turn(
    native: Native,
    monkeypatch: pytest.MonkeyPatch,
    spawned: bool,
) -> None:
    prepared = startup.prepare(
        native.directory,
        "Existing",
        launch.resume_arguments(native.directory, "Existing", THREAD),
        "tj",
        "fixture-codex",
        {},
    )
    rollout = native.directory / "rollout.jsonl"
    rollout.unlink()
    observed: dict[str, Liveness] = {}
    calls: list[tuple[str, str]] = []

    def record_prepared(session: str, launch_id: str) -> bool:
        calls.append((session, launch_id))
        if spawned:
            observed[session] = Liveness(
                True, process_ids=(22,), launch_ids=(launch_id,)
            )
        return spawned

    monkeypatch.setattr(startup.processes, "inventory", lambda **_options: observed)
    monkeypatch.setattr(startup.processes, "record_prepared", record_prepared)
    monkeypatch.setattr(
        agent_store,
        "run",
        lambda *_args: {"launch": {"launch_id": "replacement", "state": "active"}},
    )
    if spawned:
        startup.confirm(prepared, [], "replacement", timeout=0, clock=lambda: 0)
    else:
        with pytest.raises(LaunchTimeout, match="owned native process"):
            startup.confirm(prepared, [], "replacement", timeout=0, clock=lambda: 0)
    assert calls == [(THREAD, "replacement")]
    assert not rollout.exists()


def test_saved_live_process_cannot_confirm_a_superseded_binding(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = PreparedLaunch(
        ("fixture-codex", "--cd", str(native.directory)), f"codex:{THREAD}"
    )

    def refused(*_args: str) -> bool:
        raise LaunchError("native session is not reciprocally bound to this launch")

    monkeypatch.setattr(startup.processes, "record_prepared", refused)
    with pytest.raises(LaunchError, match="not reciprocally bound"):
        startup.confirm(prepared, [], "launch", timeout=0)


@pytest.mark.parametrize(
    "prompt", ["Original prompt", "--help", "--model=other", "-", " "]
)
def test_launch_uses_exact_session_and_genuine_native_argv(
    native: Native, prompt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        assert native.events[-1] == "disconnect"
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(agent_cli, "host_launch", lambda _: launch)
    monkeypatch.setattr(shutil, "which", lambda name: name)
    monkeypatch.setattr(agent_cli, "IS_WINDOWS", True)
    monkeypatch.setattr(uuid, "uuid4", lambda: SimpleNamespace(hex="exact-launch"))
    monkeypatch.setattr(
        agent_cli,
        "await_launch",
        lambda *args, **kwargs: {
            "launch": {"state": "active"},
            "session": {"session": f"codex:{THREAD}", "launch_id": "exact-launch"},
        },
    )
    arguments = launch.start_arguments(
        native.directory, "Actual title", prompt, "fixture-model", "high"
    )
    call_launch = cast(Callable[..., dict[str, object]], agent_cli.launch)
    call_launch(Host.CODEX, native.directory, "Actual title", arguments, run=run)
    command = commands[0]
    assert command[command.index("--session") + 1] == f"codex:{THREAD}"
    assert command[-13:] == [
        "fixture-codex",
        "--cd",
        str(native.directory),
        "resume",
        THREAD,
        "--model",
        "fixture-model",
        "-c",
        "model_reasoning_effort=high",
        "--remote",
        "unix://",
        "--",
        prompt,
    ]
    assert command.count(prompt) == 1


@pytest.mark.parametrize("effort", [None, "high"])
def test_native_hook_trust_and_tool_permissions_are_preserved_on_thread_start(
    native: Native, monkeypatch: pytest.MonkeyPatch, effort: str | None
) -> None:
    request = native.request
    hook_attempts: list[dict[str, object]] = []

    def request_with_hook(method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "thread/start":
            config = params.get("config")
            assert isinstance(config, dict)
            assert "bypass_hook_trust" not in config
            assert config.get("model_reasoning_effort") == effort
            assert "approvalPolicy" not in params and "sandbox" not in params
            hook_attempts.append(params)
        return request(method, params)

    monkeypatch.setattr(native, "request", request_with_hook)
    prompt = "Keep --dangerously-bypass-hook-trust as literal prompt text"
    arguments = launch.start_arguments(
        native.directory, "Title", prompt, "fixture-model", effort
    )
    prepared = startup.prepare(
        native.directory, "Title", arguments, "tj", "fixture-codex", {}
    )
    assert len(hook_attempts) == 1
    assert prepared.session == f"codex:{THREAD}"
    options = prepared.command[: prepared.command.index("--")]
    assert "--dangerously-bypass-hook-trust" not in options
    assert options[-2:] == ("--remote", "unix://")
    assert prepared.command[-1] == prompt


@pytest.mark.parametrize("dispatched", [False, True])
def test_launch_cleans_only_after_definite_failure(
    native: Native, dispatched: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare(native)
    calls: list[tuple[str, ...]] = []
    receipt: dict[str, object] = {
        "launch": {
            "launch_id": "exact-launch",
            "host": "codex",
            "state": "ended",
            "end_reason": "not_started",
            "session": None,
            "wrapper": {"pid": 42, "started_at": "2026-11-11T00:00:00Z"},
        },
        "session": None,
    }

    def store(*arguments: str) -> dict[str, object]:
        calls.append(arguments)
        return receipt

    monkeypatch.setattr(agent_store, "run", store)
    startup.abandon(prepared, "exact-launch", dispatched)
    assert native.events[-2:] == [
        ("thread/archive", {"threadId": THREAD}),
        "disconnect",
    ]
    assert calls == ([("show", "--launch-id", "exact-launch")] if dispatched else [])


@pytest.mark.parametrize(
    "change",
    [
        {"end_reason": "exited"},
        {"state": "active"},
        {"launch_id": "other"},
        {"host": "claude"},
        {"session": "codex:other"},
        {"wrapper": None},
    ],
)
def test_abandon_preserves_threads_when_failure_receipt_is_uncertain(
    native: Native, change: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare(native)
    before = list(native.events)
    record = {
        "launch_id": "exact-launch",
        "host": "codex",
        "state": "ended",
        "end_reason": "not_started",
        "session": None,
        "wrapper": {"pid": 42, "started_at": "2026-11-11T00:00:00Z"},
        **change,
    }
    monkeypatch.setattr(
        agent_store, "run", lambda *args: {"launch": record, "session": None}
    )
    startup.abandon(prepared, "exact-launch", True)
    assert native.events == before


def test_abandon_preserves_thread_when_dispatch_times_out_without_receipt(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare(native)
    before = list(native.events)

    def store(*arguments: str) -> dict[str, object]:
        raise agent_store.AgentStoreError("not-found", "no receipt")

    monkeypatch.setattr(agent_store, "run", store)
    startup.abandon(prepared, "exact-launch", True)
    assert native.events == before


@pytest.mark.parametrize("resuming", [False, True])
def test_prepare_requires_proven_native_version_before_any_creation(
    native: Native, monkeypatch: pytest.MonkeyPatch, resuming: bool
) -> None:
    def resolve(*, minimum: str) -> None:
        assert minimum == "0.169.0"
        raise LaunchError("below the required 0.169.0")

    monkeypatch.setattr(launch, "resolve_executable", resolve)
    arguments = (
        launch.resume_arguments(native.directory, "Title", THREAD)
        if resuming
        else launch.start_arguments(native.directory, "Title", "Prompt", None, None)
    )
    with pytest.raises(LaunchError, match=r"required 0\.169\.0"):
        launch.prepare(native.directory, "Title", arguments, "tj", {})
    assert native.events == []


@pytest.mark.parametrize("dispatched", [False, True])
def test_launch_failure_cleans_only_before_dispatch_without_store_proof(
    native: Native, dispatched: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def open_tab(*arguments: object, **options: object) -> None:
        if dispatched:
            run = cast(Callable[..., object], arguments[6])
            run(["fake-terminal"])
        raise LaunchError("terminal failed")

    def store(*arguments: str) -> dict[str, object]:
        raise agent_store.AgentStoreError("not-found", "no verified launch receipt")

    monkeypatch.setattr(agent_cli, "host_launch", lambda _: launch)
    monkeypatch.setattr(shutil, "which", lambda name: name)
    monkeypatch.setattr(agent_cli, "open_tab", open_tab)
    monkeypatch.setattr(agent_store, "run", store)
    arguments = launch.start_arguments(native.directory, "Title", "Prompt", None, None)
    call_launch = cast(Callable[..., dict[str, object]], agent_cli.launch)
    with pytest.raises(LaunchError, match="terminal failed"):
        call_launch(
            Host.CODEX,
            native.directory,
            "Title",
            arguments,
            run=lambda *args, **kwargs: None,
        )
    archived = ("thread/archive", {"threadId": THREAD}) in native.events
    assert archived is not dispatched


def test_prepare_stops_when_repository_admission_fails(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object) -> None:
        raise LaunchError("admission refused")

    monkeypatch.setattr(startup, "admit", refuse)
    with pytest.raises(LaunchError, match="admission refused"):
        prepare(native)
    assert native.events == [["tj", "_session", "--help"]]


def test_prepare_archives_exact_created_thread_when_creator_disconnect_fails(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    @contextmanager
    def connect(executable: str, directory: Path) -> Iterator[Native]:
        nonlocal attempts
        attempts += 1
        with native.connect(executable, directory) as server:
            yield server
        if attempts == 1:
            raise RuntimeError("creator disconnect failed")

    monkeypatch.setattr(startup, "connection", connect)
    with pytest.raises(LaunchError, match="creator disconnect failed"):
        prepare(native)
    assert native.events[-2:] == [
        ("thread/archive", {"threadId": THREAD}),
        "disconnect",
    ]
    assert attempts == 2


@pytest.mark.parametrize("mismatch", [False, True])
def test_abandon_requires_reciprocal_not_started_binding_before_archive_and_end(
    native: Native, mismatch: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare(native)
    before = list(native.events)
    wrapper = {"pid": 42, "started_at": "2026-11-11T00:00:00Z"}
    receipt: dict[str, object] = {
        "launch": {
            "launch_id": "exact-launch",
            "host": "codex",
            "state": "ended",
            "end_reason": "not_started",
            "session": prepared.session,
            "wrapper": wrapper,
        },
        "session": {
            "session": prepared.session,
            "host": "codex",
            "native_id": THREAD,
            "launch_id": "exact-launch",
            "wrapper": wrapper,
        },
    }
    calls: list[tuple[str, ...]] = []

    def store(*arguments: str) -> dict[str, object]:
        calls.append(arguments)
        if arguments == ("show", prepared.session) and mismatch:
            return {"launch": None, "session": None}
        return receipt

    monkeypatch.setattr(agent_store, "run", store)
    startup.abandon(prepared, "exact-launch", True)
    assert calls[:2] == [
        ("show", "--launch-id", "exact-launch"),
        ("show", str(prepared.session)),
    ]
    if mismatch:
        assert len(calls) == 2 and native.events == before
    else:
        assert calls[2:] == [
            ("end", "--session", str(prepared.session), "--reason", "not_started")
        ]
        assert native.events[-2:] == [
            ("thread/archive", {"threadId": THREAD}),
            "disconnect",
        ]


def test_prepare_reports_name_failure_and_secondary_archive_failure(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = RuntimeError("native naming failed")
    request = native.request

    def fail(method: str, parameters: dict[str, object]) -> dict[str, object]:
        if method == "thread/name/set":
            raise primary
        if method == "thread/archive":
            raise OSError("native archive failed")
        return request(method, parameters)

    monkeypatch.setattr(native, "request", fail)
    with pytest.raises(LaunchError, match="native naming failed") as failure:
        prepare(native)
    assert failure.value.__cause__ is primary
    output = io.StringIO()
    report = cast(Callable[..., int], agent_cli.report_launch_failure)
    assert report(failure.value, err=output) == 1
    assert "native naming failed" in output.getvalue()
    assert "Launch cleanup failed (OSError): native archive failed" in output.getvalue()


def test_create_keeps_settings_failure_when_exact_thread_archive_also_fails(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    native.model = "unexpected-model"
    request = native.request

    def fail_archive(method: str, parameters: dict[str, object]) -> dict[str, object]:
        if method == "thread/archive":
            raise RuntimeError("created thread archive failed")
        return request(method, parameters)

    monkeypatch.setattr(native, "request", fail_archive)
    with pytest.raises(LaunchError, match="different launch settings") as failure:
        prepare(native)
    assert failure.value.__cause__ is None
    output = io.StringIO()
    report = cast(Callable[..., int], agent_cli.report_launch_failure)
    assert report(failure.value, err=output) == 1
    assert "created thread archive failed" in output.getvalue()


def test_prepare_reports_creator_disconnect_and_secondary_cleanup_failure(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = RuntimeError("creator disconnect failed")
    attempts = 0

    @contextmanager
    def connect(executable: str, directory: Path) -> Iterator[Native]:
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            raise RuntimeError("cleanup connection failed")
        with native.connect(executable, directory) as server:
            yield server
        raise primary

    monkeypatch.setattr(startup, "connection", connect)
    with pytest.raises(LaunchError, match="creator disconnect failed") as failure:
        prepare(native)
    assert failure.value.__cause__ is primary
    output = io.StringIO()
    report = cast(Callable[..., int], agent_cli.report_launch_failure)
    assert report(failure.value, err=output) == 1
    assert "creator disconnect failed" in output.getvalue()
    assert "cleanup connection failed" in output.getvalue()
    assert attempts == 2


@pytest.mark.parametrize("phase", ["terminal", "timeout"])
def test_launch_keeps_primary_failure_and_timeout_exit_after_cleanup_failure(
    native: Native, phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = (
        LaunchTimeout("launch confirmation timed out")
        if phase == "timeout"
        else LaunchError("terminal launch failed")
    )
    request = native.request

    def fail_archive(method: str, parameters: dict[str, object]) -> dict[str, object]:
        if method == "thread/archive":
            raise OSError("archive cleanup failed")
        return request(method, parameters)

    def open_tab(*arguments: object, **options: object) -> None:
        if phase == "terminal":
            raise primary
        run = cast(Callable[..., object], arguments[6])
        run(["fake-terminal"])

    def await_launch(*arguments: object, **options: object) -> None:
        raise primary

    def store(*arguments: str) -> dict[str, object]:
        raise agent_store.AgentStoreError("unavailable", "Store cleanup failed")

    monkeypatch.setattr(native, "request", fail_archive)
    monkeypatch.setattr(agent_cli, "host_launch", lambda _: launch)
    monkeypatch.setattr(shutil, "which", lambda name: name)
    monkeypatch.setattr(agent_cli, "open_tab", open_tab)
    monkeypatch.setattr(agent_cli, "await_launch", await_launch)
    monkeypatch.setattr(agent_store, "run", store)
    arguments = launch.start_arguments(native.directory, "Title", "Prompt", None, None)
    call_launch = cast(Callable[..., dict[str, object]], agent_cli.launch)
    with pytest.raises(LaunchError, match=str(primary)) as failure:
        call_launch(
            Host.CODEX,
            native.directory,
            "Title",
            arguments,
            run=lambda *args, **kwargs: None,
        )
    assert failure.value is primary
    output = io.StringIO()
    report = cast(Callable[..., int], agent_cli.report_launch_failure)
    assert report(failure.value, err=output) == (3 if phase == "timeout" else 1)
    assert str(primary) in output.getvalue()
    assert (
        "Store cleanup failed" if phase == "timeout" else "archive cleanup failed"
    ) in output.getvalue()
    if phase == "timeout":
        assert "check the terminal before launching another" in output.getvalue()
