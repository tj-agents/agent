from __future__ import annotations

import io
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
from launch_contract import PreparedLaunch
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

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.events.append((method, params))
        if method == self.failure:
            raise RuntimeError(f"failed {method}")
        if method == "config/read":
            return {"config": {"projects": self.projects}, "layers": []}
        if method == "thread/start":
            return {
                "thread": {"id": self.result_id},
                "cwd": str(self.directory),
                "model": self.model,
                "reasoningEffort": self.effort,
            }
        if method in {"thread/name/set", "thread/archive"}:
            return {}
        pytest.fail(f"Unexpected native request: {method}")

    @contextmanager
    def connect(self, native: str, directory: Path) -> Iterator[Native]:
        assert native == "fixture-codex" and directory == self.directory
        self.events.append("connect")
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


@pytest.fixture
def native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Native:
    fake = Native(tmp_path, projects={str(tmp_path): {"trust_level": "trusted"}})

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        fake.events.append(command)
        if command == ["tj", "run", "--help"]:
            return subprocess.CompletedProcess(
                command, 0, "  --session <SESSION>\n", ""
            )
        assert command == ["fixture-codex", "app-server", "daemon", "start"]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(process, "run", run)
    monkeypatch.setattr(startup, "connection", fake.connect)
    monkeypatch.setattr(startup, "admit", lambda *args: fake.events.append("admit"))
    monkeypatch.setattr(startup.projects, "user_config", dict)
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
            native.directory, "Actual title", arguments, "tj", "fixture-codex"
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
            "--",
            'Read "file"; $value λ',
        ),
        f"codex:{THREAD}",
        created=True,
    )
    assert native.events == [
        ["tj", "run", "--help"],
        "admit",
        ["fixture-codex", "app-server", "daemon", "start"],
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
    assert "-c" not in prepared.command and "--model" not in prepared.command


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
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "Usage: tj run", ""),
    )
    with pytest.raises(LaunchError, match="tj run --session"):
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


def test_prepare_existing_resume_does_not_create_or_archive(native: Native) -> None:
    prepared = startup.prepare(
        native.directory,
        "Existing",
        launch.resume_arguments(native.directory, "Existing", THREAD),
        "tj",
        "fixture-codex",
    )
    startup.abandon(prepared, "new-launch", False)
    assert prepared.command == (
        "fixture-codex",
        "--cd",
        str(native.directory),
        "resume",
        THREAD,
    )
    assert prepared.session == f"codex:{THREAD}" and not prepared.created
    assert native.events == [["tj", "run", "--help"]]


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
    assert command[-7:] == [
        "fixture-codex",
        "--cd",
        str(native.directory),
        "resume",
        THREAD,
        "--",
        prompt,
    ]
    assert command.count(prompt) == 1


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


def test_prepare_requires_proven_native_version_before_any_creation(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    def resolve(*, minimum: str) -> None:
        assert minimum == "0.167.1"
        raise LaunchError("below the required 0.167.1")

    monkeypatch.setattr(launch, "resolve_executable", resolve)
    arguments = launch.start_arguments(native.directory, "Title", "Prompt", None, None)
    with pytest.raises(LaunchError, match=r"required 0\.167\.1"):
        launch.prepare(native.directory, "Title", arguments, "tj")
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
    assert native.events == [["tj", "run", "--help"]]


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


REFUSED = "Error: this Windows launcher prevents background processes from outliving it"


def daemon_runs(
    monkeypatch: pytest.MonkeyPatch,
    start: subprocess.CompletedProcess[str],
    up_after: int,
) -> list[list[str]]:
    calls: list[list[str]] = []
    versions = iter([1] * up_after + [0])

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[-1] == "start" and command[0] == "fixture-codex":
            return start
        if command[-1] == "version":
            return subprocess.CompletedProcess(command, next(versions), "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(process, "run", run)
    monkeypatch.setattr(shutil, "which", lambda name: f"C:/bin/{name}")
    monkeypatch.setattr(startup, "IS_WINDOWS", True)
    return calls


def test_start_daemon_opens_a_terminal_tab_when_the_callers_job_refuses_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    refused = subprocess.CompletedProcess(["start"], 1, "", REFUSED)
    calls = daemon_runs(monkeypatch, refused, up_after=2)
    waits: list[float] = []
    startup.start_daemon(
        "fixture-codex", tmp_path, clock=lambda: 0.0, wait=waits.append
    )
    tab = ["C:/bin/wt.exe", "-w", "0", "nt", "--title", "Codex daemon"]
    assert calls[1] == [*tab, "fixture-codex", "app-server", "daemon", "start"]
    assert [call[-1] for call in calls[2:]] == ["version", "version", "version"]
    assert waits == [1, 1]


def test_start_daemon_reports_a_tab_that_never_brings_it_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    refused = subprocess.CompletedProcess(["start"], 1, "", REFUSED)
    daemon_runs(monkeypatch, refused, up_after=100)
    ticks = iter(range(0, 1000, 10))
    with pytest.raises(LaunchError, match="did not start from a Windows Terminal tab"):
        startup.start_daemon(
            "fixture-codex",
            tmp_path,
            clock=lambda: float(next(ticks)),
            wait=lambda _: None,
        )


@pytest.mark.parametrize("windows", [True, False])
def test_start_daemon_reports_other_failures_without_a_tab(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, windows: bool
) -> None:
    error = REFUSED if not windows else "Error: config is invalid"
    calls = daemon_runs(
        monkeypatch, subprocess.CompletedProcess(["start"], 1, "", error), 0
    )
    monkeypatch.setattr(startup, "IS_WINDOWS", windows)
    with pytest.raises(LaunchError, match="Cannot start the configured Codex daemon"):
        startup.start_daemon("fixture-codex", tmp_path)
    assert len(calls) == 1
