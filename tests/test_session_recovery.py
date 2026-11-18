import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from claude import launch as claude_launch
from claude import sessions as claude_sessions
from codex import launch as codex_launch
from codex import observer as codex_observer
from codex import sessions as codex_sessions
from launch_contract import Liveness
from launch_errors import LaunchError


def test_claude_transcript_lookup_uses_session_identity_and_configured_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project = tmp_path / "projects" / "native-encoded-project"
    project.mkdir(parents=True)
    selected = project / "exact-session.jsonl"
    selected.write_text("{}\n", encoding="utf-8")
    unrelated = project / "other.jsonl"
    unrelated.write_text("{}\n", encoding="utf-8")
    os.utime(selected, (101, 101))
    assert claude_sessions.transcript_modified("exact-session") == 101
    with pytest.raises(FileNotFoundError, match="not found"):
        claude_sessions.transcript_modified("unknown")


@pytest.mark.parametrize(
    "file_modified,index_modified,expected", [(99, 101, 101), (102, 99, 102)]
)
def test_codex_native_index_locates_transcript_and_includes_paginated_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_modified: float,
    index_modified: int,
    expected: float,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    transcript = tmp_path / "host-recorded-name.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    os.utime(transcript, (file_modified, file_modified))
    with closing(sqlite3.connect(tmp_path / "state_5.sqlite")) as database:
        database.execute(
            "CREATE TABLE threads (id TEXT, rollout_path TEXT, updated_at INTEGER)"
        )
        database.execute(
            "INSERT INTO threads VALUES (?, ?, ?)",
            ("thread", str(transcript), index_modified),
        )
        database.commit()
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert codex_sessions.transcript_modified("thread") == expected
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    with pytest.raises(FileNotFoundError, match="not found"):
        codex_sessions.transcript_modified("missing")


def test_codex_missing_index_never_guesses_a_transcript_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="index not found"):
        codex_sessions.transcript_modified("missing")


def test_claude_failed_probe_is_distinct_from_empty_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_launch, "executable", lambda: "fake-agent")

    def failed(*arguments: Any, **options: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr="probe failed")

    with pytest.raises(LaunchError, match="probe failed"):
        claude_launch.liveness(run=failed, strict=True)
    assert (
        claude_launch.liveness(
            run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="[]"), strict=True
        )
        == {}
    )


@pytest.mark.parametrize("code", [2, 111, 10061])
def test_codex_absent_daemon_is_dead_without_opening_server(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    monkeypatch.setattr(codex_launch, "executable", lambda: "fake-agent")

    def run(*arguments: Any, **options: Any) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"failed to connect to native control socket (os error {code})",
        )

    def daemon() -> None:
        raise AssertionError("absent daemon must not be opened")

    monkeypatch.setattr(codex_launch.process, "run", run)
    monkeypatch.setattr(codex_launch, "daemon", daemon)
    assert codex_launch.liveness(strict=True) == {}


@pytest.mark.parametrize("endpoint_exists", [False, True])
def test_codex_network_failure_requires_proof_that_the_endpoint_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint_exists: bool,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    endpoint = codex_observer.socket_path(tmp_path)
    if endpoint_exists:
        endpoint.parent.mkdir()
        endpoint.touch()
    expected = {"embedded": Liveness(True, process_ids=(42,), launch_ids=("owned",))}
    monkeypatch.setattr(codex_launch.processes, "inventory", lambda: expected)
    monkeypatch.setattr(codex_launch, "executable", lambda: "fake-agent")
    monkeypatch.setattr(
        codex_launch.process,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="",
            stderr=f"failed to connect to {endpoint}: dead network (os error 10050)",
        ),
    )
    if endpoint_exists:
        with pytest.raises(LaunchError, match="dead network"):
            codex_launch.liveness(strict=True)
    else:
        assert codex_launch.liveness(strict=True) == expected


def test_codex_inaccessible_endpoint_is_not_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_launch.processes, "inventory", dict)
    monkeypatch.setattr(codex_launch, "executable", lambda: "fake-agent")
    monkeypatch.setattr(
        codex_launch.process,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="",
            stderr="failed to connect to native control socket (os error 10050)",
        ),
    )

    def denied(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("Cannot inspect control socket")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "stat", denied)
        with pytest.raises(LaunchError, match="Cannot inspect control socket"):
            codex_launch.liveness(strict=True)


@pytest.mark.parametrize("probe_failure", [True, False])
def test_codex_failed_daemon_probe_or_thread_read_is_not_absence(
    monkeypatch: pytest.MonkeyPatch, probe_failure: bool
) -> None:
    monkeypatch.setattr(codex_launch, "executable", lambda: "fake-agent")

    def run(*arguments: Any, **options: Any) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1 if probe_failure else 0,
            stdout="{}",
            stderr="permission denied",
        )

    class Server:
        def __enter__(self) -> "Server":
            return self

        def __exit__(self, *arguments: Any) -> None:
            pass

        def request(self, method: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if method == "thread/loaded/list":
                return {"data": ["thread"]}
            raise RuntimeError("thread read failed")

    monkeypatch.setattr(codex_launch.process, "run", run)
    monkeypatch.setattr(codex_launch, "daemon", Server)
    with pytest.raises(LaunchError, match=r"permission denied|thread read failed"):
        codex_launch.liveness(strict=True)


def test_codex_default_probe_keeps_status_working_without_native_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def daemon() -> None:
        raise LaunchError("native executable unavailable")

    monkeypatch.setattr(codex_launch, "daemon", daemon)
    assert codex_launch.liveness() == {}


@pytest.mark.parametrize("reverse", [False, True])
def test_claude_liveness_preserves_background_and_all_processes(
    monkeypatch: pytest.MonkeyPatch, reverse: bool
) -> None:
    monkeypatch.setattr(claude_launch, "executable", lambda: "fake-agent")
    rows = [
        {"sessionId": "thread", "kind": "background", "id": "bg", "pid": 42},
        {"sessionId": "thread", "pid": 43},
    ]
    if reverse:
        rows.reverse()
    result = SimpleNamespace(returncode=0, stdout=json.dumps(rows))
    live = claude_launch.liveness(run=lambda *args, **kwargs: result, strict=True)
    assert live["thread"].background == "bg"
    assert live["thread"].process_ids == (42, 43)


@pytest.mark.parametrize("pid", [0, -1, True, "42", None])
def test_claude_invalid_pid_invalidates_entire_inventory(
    monkeypatch: pytest.MonkeyPatch, pid: object
) -> None:
    monkeypatch.setattr(claude_launch, "executable", lambda: "fake-agent")
    rows = [{"sessionId": "valid", "pid": 12}, {"sessionId": "bad", "pid": pid}]
    result = SimpleNamespace(returncode=0, stdout=json.dumps(rows))

    def run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return result

    assert claude_launch.liveness(run=run) == {}
    with pytest.raises(LaunchError, match="invalid process id"):
        claude_launch.liveness(run=run, strict=True)
