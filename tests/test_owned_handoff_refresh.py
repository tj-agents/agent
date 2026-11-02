import hashlib
import importlib
import importlib.util
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_source(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class Refresh:
    sessions: Any
    watcher: Any
    args: Any
    binding: dict[str, Any]
    record: dict[str, Any]
    lifecycle: dict[str, Any]
    client_path: Path
    receipt_path: Path
    statuses: dict[int, str]

    def save(self) -> None:
        self.args.record.write_text(json.dumps(self.record), encoding="utf-8")
        self.receipt_path.write_text(json.dumps(self.lifecycle), encoding="utf-8")

    def closed(self) -> bool:
        return bool(
            self.sessions.backend_closed(self.binding, self.watcher.session_close)
        )

    def wait(self) -> bool:
        clock = iter((0.0, 0.1, 2.0))
        return bool(
            self.watcher.wait_for_exit(
                self.args,
                exited=lambda *_args: True,
                clock=lambda: next(clock),
                sleep=lambda _seconds: None,
            )
        )


@pytest.fixture
def refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Refresh:
    runtime = ROOT / "plugins/agent/.agents/agent/runtime"
    monkeypatch.syspath_prepend(str(runtime))
    monkeypatch.syspath_prepend(
        str(ROOT / "plugins/agent/.agents/agent/utility/peer/scripts")
    )
    sessions = load_source(
        "owned_refresh_sessions", ROOT / ".codex/runtime/sessions.py"
    )
    watcher = load_source(
        "owned_refresh_watcher", ROOT / ".agents/agent/runtime/relaunch_watcher.py"
    )
    host = importlib.import_module("host")
    hosts = importlib.import_module("host_sessions")
    monkeypatch.setitem(hosts.SESSIONS, host.Host.CODEX, sessions)
    root = tmp_path / "refresh checkout café"
    root.mkdir()
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(root / "state"))
    binding: dict[str, Any] = {
        "token": "a" * 64,
        "host": "codex",
        "profile": str(root / "profile"),
        "launch_cwd": str(root),
        "recording_path": str(root / "events.jsonl"),
        "executable": str(root / "native-codex"),
        "frontend": {"pid": 104, "started_at": 104.0},
        "wrapper": {"pid": 101, "started_at": 101.0},
        "supervisor": {"pid": 102, "started_at": 102.0},
        "backend": {
            "kind": "handoff",
            "pid": 103,
            "started_at": 103.0,
            "socket_path": str(root / "old socket"),
            "socket_identity": [1, 123],
        },
        "recipient": {
            "thread_id": "thread",
            "cwd": str(root),
            "frontend_message_id": "anchor",
        },
        "frontend_message_id": "anchor",
    }
    client_path = Path(sessions.client.binding_path(binding["token"]))
    client_path.parent.mkdir(parents=True)
    client_path.write_text(
        json.dumps(
            {
                key: value
                for key, value in binding.items()
                if key != "frontend_message_id"
            }
        ),
        encoding="utf-8",
    )
    lifecycle = {
        "state": "exited",
        "token": binding["token"],
        "wrapper": binding["wrapper"],
        "supervisor": binding["supervisor"],
        "backend": binding["backend"],
    }
    request = root / "close-request.json"
    request.write_text(
        json.dumps(
            {
                "entry": {
                    "backend": binding["backend"],
                    "launch_token": binding["token"],
                    "pid": binding["frontend"]["pid"],
                }
            }
        ),
        encoding="utf-8",
    )
    result = root / "close-result.json"
    result.write_text(
        json.dumps({"status": "closed", "session_id": "thread"}), encoding="utf-8"
    )
    record = {
        "token": "refresh-token",
        "state": "waiting",
        "cwd": str(root),
        "owned_backend": binding,
        "close": {
            "request": str(request),
            "result": str(result),
            "request_sha256": hashlib.sha256(request.read_bytes()).hexdigest(),
        },
    }
    digest = hashlib.sha256(
        json.dumps(binding, sort_keys=True).encode("utf-8")
    ).hexdigest()
    args = watcher.arguments(
        [
            "--host",
            "codex",
            "--session-id",
            "thread",
            "--pid",
            "104",
            "--pid-started-at",
            "104.0",
            "--record",
            str(root / "refresh.json"),
            "--token",
            "refresh-token",
            "--recovery",
            str(root / "recover.py"),
            "--title",
            "Fixture",
            "--prompt",
            "Continue",
            "--expected-cwd",
            str(root),
            "--timeout",
            "1",
            "--owned-backend-binding",
            digest,
            "--history-root",
            binding["profile"],
            "--native-argument=--model",
            "--native-argument=fixture-model",
        ]
    )
    statuses = {102: "exited", 103: "exited"}
    monkeypatch.setattr(
        watcher.session_close, "identity_status", lambda pid, _started: statuses[pid]
    )
    fixture = Refresh(
        sessions,
        watcher,
        args,
        binding,
        record,
        lifecycle,
        client_path,
        client_path.with_name("backend.json"),
        statuses,
    )
    fixture.save()
    return fixture


@pytest.mark.parametrize("attached", [False, True], ids=["split", "attached"])
def test_refresh_removes_exactly_the_owned_remote_option(
    refresh: Refresh, attached: bool
) -> None:
    endpoint = "unix://" + refresh.binding["backend"]["socket_path"]
    remote = ["--remote=" + endpoint] if attached else ["--remote", endpoint]
    assert refresh.sessions.native_arguments(
        ["--model", "fixture-model", *remote, "resume", "thread", "Continue"],
        refresh.binding,
    ) == ["--model", "fixture-model"]


@pytest.mark.parametrize(
    "problem", ["absent", "missing-value", "different", "relative", "duplicate"]
)
def test_refresh_refuses_missing_ambiguous_or_foreign_remote_selection(
    refresh: Refresh, problem: str
) -> None:
    endpoint = "unix://" + refresh.binding["backend"]["socket_path"]
    arguments = {
        "absent": [],
        "missing-value": ["--remote"],
        "different": ["--remote", endpoint + "-other"],
        "relative": ["--remote=unix://relative/socket"],
        "duplicate": ["--remote", endpoint, "--remote=" + endpoint],
    }[problem]
    with pytest.raises(RuntimeError, match=r"endpoint|exactly one"):
        refresh.sessions.native_arguments(arguments, refresh.binding)


def test_refresh_relaunches_owned_backend_only_after_complete_cleanup(
    refresh: Refresh,
) -> None:
    assert refresh.closed()
    assert refresh.wait()
    run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "ready", ""))
    assert refresh.watcher.launch(refresh.args, run=run) == 0
    arguments = run.call_args.args[0]
    assert "--owned-backend" in arguments
    assert "--native-argument=--model" in arguments
    assert (
        arguments[arguments.index("--codex-history-root") + 1]
        == refresh.binding["profile"]
    )
    assert not any("--remote" in value for value in arguments)
    assert (
        json.loads(refresh.args.record.read_text(encoding="utf-8"))["state"]
        == "launched"
    )


@pytest.mark.parametrize("pid", [102, 103], ids=["supervisor", "backend"])
@pytest.mark.parametrize("status", ["live", "reused", "unknown"])
def test_refresh_waits_for_both_frozen_processes_to_exit(
    refresh: Refresh, pid: int, status: str
) -> None:
    refresh.statuses[pid] = status
    assert not refresh.closed()
    assert not refresh.wait()
    assert (
        json.loads(refresh.args.record.read_text(encoding="utf-8"))["state"]
        == "expired"
    )


@pytest.mark.parametrize(
    "changed",
    [
        "missing-receipt",
        "missing-client",
        "invalid-receipt",
        "non-object-receipt",
        "non-object-client",
        "state",
        "token",
        "wrapper",
        "supervisor",
        "backend",
        "client",
        "socket",
    ],
)
def test_missing_or_changed_cleanup_evidence_never_launches(
    refresh: Refresh, changed: str
) -> None:
    if changed == "missing-receipt":
        refresh.receipt_path.unlink()
    elif changed == "missing-client":
        refresh.client_path.unlink()
    elif changed == "invalid-receipt":
        refresh.receipt_path.write_text("{", encoding="utf-8")
    elif changed == "non-object-receipt":
        refresh.receipt_path.write_text("[]", encoding="utf-8")
    elif changed == "non-object-client":
        refresh.client_path.write_text("null", encoding="utf-8")
    elif changed == "client":
        record = json.loads(refresh.client_path.read_text(encoding="utf-8"))
        record["recipient"]["thread_id"] = "other-thread"
        refresh.client_path.write_text(json.dumps(record), encoding="utf-8")
    elif changed == "socket":
        Path(refresh.binding["backend"]["socket_path"]).touch()
    else:
        receipt = json.loads(refresh.receipt_path.read_text(encoding="utf-8"))
        if changed in ("state", "token"):
            receipt[changed] = "changed"
        else:
            receipt[changed]["started_at"] += 1
        refresh.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert not refresh.closed()
    run = mock.Mock()
    assert refresh.watcher.launch(refresh.args, run=run) == 1
    run.assert_not_called()
    result = json.loads(refresh.args.record.read_text(encoding="utf-8"))
    assert result["state"] == "failed"
    assert "cleanup is unconfirmed" in result["error"]


def test_refresh_rechecks_cleanup_after_waiting_and_before_launch(
    refresh: Refresh,
) -> None:
    assert refresh.wait()
    refresh.lifecycle["state"] = "running"
    refresh.save()
    run = mock.Mock()
    assert refresh.watcher.launch(refresh.args, run=run) == 1
    run.assert_not_called()


@pytest.mark.parametrize("changed", ["missing", "backend", "supervisor"])
def test_refresh_rejects_a_changed_frozen_binding(
    refresh: Refresh, changed: str
) -> None:
    record = json.loads(refresh.args.record.read_text(encoding="utf-8"))
    if changed == "missing":
        record.pop("owned_backend")
    else:
        record["owned_backend"][changed]["pid"] += 1
    refresh.args.record.write_text(json.dumps(record), encoding="utf-8")
    run = mock.Mock()
    assert refresh.watcher.launch(refresh.args, run=run) == 1
    run.assert_not_called()
    result = json.loads(refresh.args.record.read_text(encoding="utf-8"))
    assert "frozen owned backend changed" in result["error"]


@pytest.mark.parametrize("changed", ["backend", "launch_token", "pid"])
def test_refresh_close_request_must_belong_to_its_frozen_backend(
    refresh: Refresh, changed: str
) -> None:
    request = Path(refresh.record["close"]["request"])
    payload = json.loads(request.read_text(encoding="utf-8"))
    if changed == "backend":
        payload["entry"]["backend"]["pid"] += 1
    else:
        payload["entry"][changed] = "other"
    request.write_text(json.dumps(payload), encoding="utf-8")
    refresh.record["close"]["request_sha256"] = hashlib.sha256(
        request.read_bytes()
    ).hexdigest()
    refresh.save()
    assert not refresh.wait()
    result = json.loads(refresh.args.record.read_text(encoding="utf-8"))
    assert result["state"] == "cancelled"
    assert "owned backend differs" in result["reason"]
