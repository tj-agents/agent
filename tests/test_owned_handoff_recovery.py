import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
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


@pytest.fixture
def launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    runtime = ROOT / "plugins/agent/.agents/agent/runtime"
    monkeypatch.syspath_prepend(str(runtime))
    monkeypatch.syspath_prepend(
        str(ROOT / "plugins/agent/.agents/agent/utility/peer/scripts")
    )
    sessions = load_source(
        "owned_recovery_sessions", ROOT / ".codex/runtime/sessions.py"
    )
    host = importlib.import_module("host")
    hosts = importlib.import_module("host_sessions")
    monkeypatch.setitem(hosts.SESSIONS, host.Host.CODEX, sessions)
    root = tmp_path / "recovery checkout café"
    root.mkdir()
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(root / "state"))
    binding: dict[str, Any] = {
        "token": "b" * 64,
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
            "socket_path": str(root / "owned socket"),
            "socket_identity": [1, 123],
            "parent_identity": [1, 42],
        },
        "recipient": {
            "thread_id": "thread",
            "cwd": str(root),
            "frontend_message_id": "anchor",
        },
    }
    path = sessions.client.binding_path(binding["token"])
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(binding), encoding="utf-8")
    reference = {key: binding[key] for key in ("token", "recipient", "profile")}
    status = root / "launch.status.json"
    status.write_text(
        json.dumps({"state": "running", "client_binding": reference}), encoding="utf-8"
    )
    monkeypatch.setattr(sessions.client, "verify_owned", lambda _record: "anchor")
    result = {
        "launch": "verified",
        "status_path": str(status),
        "client_binding": reference,
    }
    module = load_source(
        "owned_recovery", ROOT / ".agents/agent/runtime/tj_agent/recovery.py"
    )
    item = {
        "id": "thread",
        "session": "thread",
        "host": "codex",
        "cwd": str(root),
        "profile": binding["profile"],
        "status": "closed",
        "history_file": str(root / "history.jsonl"),
    }
    monkeypatch.setattr(module, "find", lambda *_args: item)
    monkeypatch.setattr(module, "_status_path", lambda _receipt: status)
    monkeypatch.setattr(
        module, "await_pickup", lambda *_args: "registered in sessions.db"
    )
    monkeypatch.setattr(
        module, "_wrapper_identity", lambda _status: {"pid": 201, "started_at": 201.0}
    )
    monkeypatch.setattr(module, "_attach_launch_binding", lambda *_args: None)
    monkeypatch.setattr(sessions, "open_tab", mock.Mock())
    return SimpleNamespace(
        module=module,
        sessions=sessions,
        binding=binding,
        item=item,
        status=status,
        result=result,
    )


@pytest.mark.parametrize("owned", [False, True], ids=["ordinary", "owned"])
def test_recovery_returns_and_retains_only_owned_verified_receipt(
    launch: Any,
    owned: bool,
) -> None:
    recovery = launch.module
    new_launch = launch.result
    cli = mock.Mock()
    cli.resolve_tab_directory.return_value = Path(launch.item["cwd"])
    result = recovery.open_session(
        recovery.Host.CODEX, "thread", cli=cli, owned_backend=owned
    )
    assert result["launch"] == "verified"
    receipt = recovery._receipt(
        recovery.Host.CODEX, "thread", launch.binding["profile"]
    )
    record = json.loads(receipt.read_text(encoding="utf-8"))
    assert record["state"] == "launched"
    assert Path(new_launch["status_path"]).exists() == owned
    if owned:
        assert result["status_path"] == new_launch["status_path"]
        assert result["client_binding"] == new_launch["client_binding"]
        assert record["status_file"] == new_launch["status_path"]
    else:
        assert "status_path" not in result
        assert "client_binding" not in result
        assert record["status_file"] is None


def test_recovery_retains_ambiguous_receipt_and_refuses_duplicate_after_invalid_binding(
    launch: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery = launch.module
    new_launch = launch.result
    status = Path(new_launch["status_path"])
    status.write_text(json.dumps({"state": "unconfirmed"}), encoding="utf-8")
    cli = mock.Mock()
    cli.resolve_tab_directory.return_value = Path(launch.item["cwd"])
    with pytest.raises(
        recovery.LaunchUnverified, match="receipt could not be verified"
    ):
        recovery.open_session(
            recovery.Host.CODEX, "thread", cli=cli, owned_backend=True
        )
    receipt = recovery._receipt(
        recovery.Host.CODEX, "thread", launch.binding["profile"]
    )
    record = json.loads(receipt.read_text(encoding="utf-8"))
    assert record["state"] == "ambiguous"
    assert record["status_file"] == str(status)
    assert status.is_file()
    monkeypatch.setattr(recovery, "_launch_ended", lambda _receipt: False)
    with pytest.raises(ValueError, match="prior launch"):
        recovery.open_session(
            recovery.Host.CODEX, "thread", cli=cli, owned_backend=True
        )
    launch.sessions.open_tab.assert_called_once()


def test_claude_rejects_owned_backend_after_exact_recovery_lookup(
    launch: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery = launch.module
    adapter = load_source("owned_recovery_claude", ROOT / ".claude/runtime/sessions.py")
    monkeypatch.setitem(recovery.SESSIONS, recovery.Host.CLAUDE, adapter)
    find = mock.Mock(return_value={})
    monkeypatch.setattr(recovery, "find", find)
    with pytest.raises(ValueError, match="Claude recovery"):
        recovery.open_session(recovery.Host.CLAUDE, "thread", owned_backend=True)
    find.assert_called_once_with(recovery.Host.CLAUDE, "thread", None)
