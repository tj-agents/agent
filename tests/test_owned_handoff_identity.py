import importlib.util
import json
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, Self

import pytest
from tj_cli import processes

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_MESSAGE = "frontend-message"
NATIVE_MESSAGE = "latest-native-message"


@dataclass
class NativeServer:
    directory: Path
    loaded: list[str] = field(default_factory=lambda: ["thread"])
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    hooks: list[dict[str, Any]] = field(default_factory=list)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        pass

    def request(self, method: str, parameters: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, parameters))
        if method == "hooks/list":
            return {"data": [{"cwd": str(self.directory), "hooks": self.hooks}]}
        if method == "thread/loaded/list":
            return {"data": self.loaded}
        if method == "thread/read":
            return {
                "thread": {
                    "id": parameters["threadId"],
                    "cwd": str(self.directory),
                    "source": "cli",
                    "path": str(self.directory / "profile/sessions/rollout.jsonl"),
                }
            }
        if method == "thread/turns/list":
            return {"data": [{"id": "native-turn"}]}
        if method == "thread/items/list":
            identity = NATIVE_MESSAGE if "turnId" in parameters else FRONTEND_MESSAGE
            return {"data": [{"item": {"type": "userMessage", "clientId": identity}}]}
        raise AssertionError(f"Unexpected native request: {method}")


@dataclass
class OwnedLaunch:
    client: Any
    record: dict[str, Any]
    infos: dict[int, processes.ProcessInfo]
    table: dict[int, SimpleNamespace]
    contexts: dict[int, dict[str, str]]
    commands: dict[int, list[str]]
    server: NativeServer

    def save(self) -> None:
        self.client.binding_path(self.record["token"]).write_text(
            json.dumps(self.record), encoding="utf-8"
        )

    def verify(self) -> str:
        return str(self.client.verify_owned(self.record))

    def peer_state(self, pid: int) -> tuple[str, SimpleNamespace | None]:
        info = self.table.get(pid)
        return ("present", info) if info is not None else ("absent", None)


@pytest.fixture
def launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OwnedLaunch:
    monkeypatch.syspath_prepend(str(ROOT / "plugins/agent/.agents/agent/runtime"))
    spec = importlib.util.spec_from_file_location(
        "owned_client_subject", ROOT / ".codex/runtime/client.py"
    )
    assert spec is not None and spec.loader is not None
    client: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    directory = tmp_path / "owned checkout café"
    directory.mkdir()
    profile = directory / "profile"
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(directory / "state"))
    monkeypatch.setenv("CODEX_HOME", str(profile))
    token = "a" * 64
    binding = Path(client.binding_path(token))
    binding.parent.mkdir(parents=True)
    recording = binding.with_name("events.jsonl")
    recording.write_text(
        json.dumps({"dir": "meta", "kind": "session_start"})
        + "\n"
        + json.dumps(
            {
                "dir": "from_tui",
                "kind": "op",
                "payload": {"UserTurn": {"client_user_message_id": FRONTEND_MESSAGE}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    socket = directory / "socket"
    socket.touch()
    stat = socket.stat()
    parent = socket.parent.stat()
    executable = str(directory / "native executable")
    infos = {
        pid: processes.ProcessInfo(pid, parent, float(pid))
        for pid, parent in ((101, 1), (102, 101), (103, 102), (104, 101))
    }
    record: dict[str, Any] = {
        "schema_version": 2,
        "token": token,
        "host": "codex",
        "pid": 104,
        "profile": str(profile),
        "launch_cwd": str(directory),
        "recording_path": str(recording),
        "created_at": 1.0,
        "executable": executable,
        "recipient": {
            "thread_id": "thread",
            "cwd": str(directory),
            "frontend_message_id": FRONTEND_MESSAGE,
        },
    }
    for role, pid in (
        ("wrapper", 101),
        ("supervisor", 102),
        ("backend", 103),
        ("frontend", 104),
    ):
        record[role] = {"pid": pid, "started_at": float(pid)}
    record["backend"].update(
        kind="handoff",
        socket_path=str(socket),
        socket_identity=[stat.st_dev, stat.st_ino],
        parent_identity=[parent.st_dev, parent.st_ino],
    )
    table = {
        pid: SimpleNamespace(pid=pid, name="codex", started_at=info.started_at)
        for pid, info in infos.items()
        if pid in (103, 104)
    }
    contexts = {
        103: {"profile": str(profile)},
        104: {
            "profile": str(profile),
            client.RECORD_ENV: "1",
            client.RECORD_PATH_ENV: str(recording),
        },
    }
    commands = {
        103: [executable, "app-server", "--listen", "unix://" + str(socket)],
        104: [executable, "--remote", "unix://" + str(socket), "prepared prompt"],
    }
    server = NativeServer(directory)
    fixture = OwnedLaunch(client, record, infos, table, contexts, commands, server)
    monkeypatch.setattr(processes, "process_info", infos.get)
    monkeypatch.setattr(client, "process_context", contexts.__getitem__)
    monkeypatch.setattr(client, "process_arguments", commands.__getitem__)
    monkeypatch.setattr(client, "server_for", lambda *_args, **_kwargs: server)
    monkeypatch.setattr(
        client,
        "verify_owned",
        partial(client.verify_owned, table=table, peer_state=fixture.peer_state),
    )
    monkeypatch.setattr(
        client,
        "verify_owned_processes",
        partial(
            client.verify_owned_processes, table=table, peer_state=fixture.peer_state
        ),
    )
    fixture.save()
    return fixture


def test_owned_binding_verifies_exact_processes_endpoint_and_recording(
    launch: OwnedLaunch,
) -> None:
    assert launch.verify() == FRONTEND_MESSAGE


def test_observer_uses_the_owned_endpoint_before_initial_input(
    launch: OwnedLaunch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Path(launch.record["recording_path"]).write_text("", encoding="utf-8")
    selected: list[dict[str, Any]] = []

    def server_for(record: dict[str, Any], _timeout: int) -> NativeServer:
        selected.append(record)
        return launch.server

    monkeypatch.setattr(
        launch.client,
        "server_for",
        server_for,
    )
    monkeypatch.setattr(
        launch.client.hook_trust,
        "AppServer",
        lambda *_args, **_kwargs: pytest.fail("observer opened a default proxy"),
    )
    assert launch.client.observer_server(launch.record, "unused") is launch.server
    assert selected == [launch.record]


def test_observer_rejects_a_changed_durable_binding_before_any_proxy(
    launch: OwnedLaunch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = launch.client.binding_path(launch.record["token"])
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["created_at"] = 2.0
    path.write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(
        launch.client,
        "server_for",
        lambda *_args, **_kwargs: pytest.fail("observer opened a changed binding"),
    )
    with pytest.raises(RuntimeError, match="binding changed"):
        launch.client.observer_server(launch.record, "unused")


def test_supervisor_interpreter_redirector_preserves_exact_wrapper_lineage(
    launch: OwnedLaunch,
) -> None:
    launch.infos[102] = launch.infos[102]._replace(ppid=105)
    launch.infos[105] = processes.ProcessInfo(105, 101, 101.5)
    assert launch.verify() == FRONTEND_MESSAGE


@pytest.mark.parametrize("role", ["frontend", "backend", "supervisor", "wrapper"])
def test_reused_process_id_refuses_the_owned_binding(
    launch: OwnedLaunch, role: str
) -> None:
    pid = launch.record[role]["pid"]
    launch.infos[pid] = launch.infos[pid]._replace(started_at=999.0)
    with pytest.raises(RuntimeError, match="identity changed"):
        launch.verify()


@pytest.mark.parametrize("pid", [102, 103, 104])
def test_changed_parent_refuses_process_ownership(
    launch: OwnedLaunch, pid: int
) -> None:
    launch.infos[pid] = launch.infos[pid]._replace(ppid=999)
    with pytest.raises(RuntimeError, match="process ownership changed"):
        launch.verify()


def test_replaced_socket_refuses_the_same_endpoint_path(launch: OwnedLaunch) -> None:
    socket = Path(launch.record["backend"]["socket_path"])
    socket.rename(socket.with_name("retired-socket"))
    socket.touch()
    with pytest.raises(RuntimeError, match="endpoint was replaced"):
        launch.verify()


@pytest.mark.parametrize("pid", [103, 104], ids=["backend", "frontend"])
@pytest.mark.parametrize("changed", ["executable", "profile", "endpoint"])
def test_changed_native_process_configuration_refuses_the_binding(
    launch: OwnedLaunch, pid: int, changed: str
) -> None:
    if changed == "executable":
        launch.commands[pid][0] += "-other"
    elif changed == "profile":
        launch.contexts[pid]["profile"] += "-other"
    else:
        index = launch.commands[pid].index("--listen" if pid == 103 else "--remote")
        launch.commands[pid][index + 1] += "-other"
    with pytest.raises(
        RuntimeError, match=r"executable or profile changed|different endpoint"
    ):
        launch.verify()


@pytest.mark.parametrize(
    "changed", ["disabled", "different-path", "inherited-by-backend"]
)
def test_frontend_recording_must_be_exclusive(
    launch: OwnedLaunch, changed: str
) -> None:
    if changed == "disabled":
        launch.contexts[104][launch.client.RECORD_ENV] = ""
    elif changed == "different-path":
        launch.contexts[104][launch.client.RECORD_PATH_ENV] += "-other"
    else:
        launch.contexts[103][launch.client.RECORD_ENV] = "1"
    with pytest.raises(
        RuntimeError, match=r"recording changed|inherited frontend recording"
    ):
        launch.verify()


def test_second_frontend_sharing_the_recording_refuses_attribution(
    launch: OwnedLaunch,
) -> None:
    launch.table[105] = SimpleNamespace(pid=105, name="codex", started_at=105.0)
    launch.contexts[105] = dict(launch.contexts[104])
    with pytest.raises(RuntimeError, match="Multiple native processes"):
        launch.verify()


def test_bound_recipient_keeps_frontend_and_latest_native_input_separate(
    launch: OwnedLaunch,
) -> None:
    record, submitted = launch.client.bind_recipient(
        launch.record, launch.table, launch.peer_state
    )
    assert record["recipient"]["frontend_message_id"] == FRONTEND_MESSAGE
    assert submitted["frontend_message_id"] == FRONTEND_MESSAGE
    assert submitted["client_message_id"] == NATIVE_MESSAGE
    assert submitted["turn_id"] == "native-turn"
    saved = json.loads(
        launch.client.binding_path(record["token"]).read_text(encoding="utf-8")
    )
    assert saved["recipient"] == record["recipient"]


@pytest.mark.parametrize("ephemeral", [True, False, None])
def test_recipient_excludes_only_explicitly_ephemeral_native_threads(
    launch: OwnedLaunch, monkeypatch: pytest.MonkeyPatch, ephemeral: bool | None
) -> None:
    launch.server.loaded = ["thread", "title-thread"]
    request = launch.server.request

    def with_title(method: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if parameters.get("threadId") == "title-thread":
            if method == "thread/items/list":
                raise RuntimeError("title thread history is unavailable")
            result = request(method, parameters)
            if method == "thread/read" and ephemeral is not None:
                result["thread"]["ephemeral"] = ephemeral
            return result
        return request(method, parameters)

    monkeypatch.setattr(launch.server, "request", with_title)
    if ephemeral is True:
        record, submitted = launch.client.bind_recipient(
            launch.record, launch.table, launch.peer_state
        )
        assert record["recipient"]["thread_id"] == "thread"
        assert submitted["frontend_message_id"] == FRONTEND_MESSAGE
        assert submitted["client_message_id"] == NATIVE_MESSAGE
    else:
        with pytest.raises(RuntimeError, match="title thread history is unavailable"):
            launch.client.bind_recipient(launch.record, launch.table, launch.peer_state)


@pytest.mark.parametrize(
    "loaded", [[], ["thread", "other-thread"]], ids=["missing", "ambiguous"]
)
def test_recipient_requires_exactly_one_matching_loaded_thread(
    launch: OwnedLaunch, loaded: list[str]
) -> None:
    launch.server.loaded = loaded
    with pytest.raises(RuntimeError, match=r"has not reached|exactly one"):
        launch.client.bind_recipient(launch.record, launch.table, launch.peer_state)
    assert all(method != "thread/turns/list" for method, _ in launch.server.calls)


@pytest.mark.parametrize("changed", ["thread", "cwd", "profile", "recipient", "state"])
def test_launch_receipt_refuses_a_different_recipient_or_scope(
    launch: OwnedLaunch, changed: str
) -> None:
    receipt: dict[str, Any] = {
        "state": "running",
        "client_binding": {
            "token": launch.record["token"],
            "recipient": launch.record["recipient"],
        },
    }
    arguments = {
        "thread_id": "thread",
        "cwd": launch.record["launch_cwd"],
        "profile": launch.record["profile"],
    }
    if changed == "state":
        receipt["state"] = "failed"
    elif changed == "recipient":
        receipt["client_binding"]["recipient"] = {"thread_id": "other-thread"}
    else:
        arguments["thread_id" if changed == "thread" else changed] += "-other"
    path = Path(launch.record["launch_cwd"]) / "launch.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(RuntimeError, match="receipt"):
        launch.client.launch_binding(path, **arguments)
    assert not launch.server.calls


def test_frozen_binding_rejects_a_later_recipient_change(launch: OwnedLaunch) -> None:
    frozen = launch.client.freeze_binding(launch.record)
    launch.record["recipient"] = {
        **launch.record["recipient"],
        "thread_id": "other-thread",
    }
    launch.save()
    with pytest.raises(
        RuntimeError, match="Frozen handoff endpoint or process identity changed"
    ):
        launch.client.frozen_binding(frozen)


def test_recipient_binding_discovers_the_one_verified_durable_record(
    launch: OwnedLaunch,
) -> None:
    binding = launch.client.recipient_binding(
        launch.record["recipient"]["thread_id"],
        launch.record["recipient"]["cwd"],
        launch.record["profile"],
    )
    assert binding == launch.client.freeze_binding(launch.record)


@pytest.mark.parametrize("field", ["thread_id", "cwd", "profile"])
def test_recipient_binding_refuses_a_target_without_one_exact_record(
    launch: OwnedLaunch, field: str
) -> None:
    values = {
        "thread_id": launch.record["recipient"]["thread_id"],
        "cwd": launch.record["recipient"]["cwd"],
        "profile": launch.record["profile"],
    }
    values[field] += "-other"
    with pytest.raises(RuntimeError, match="exactly one"):
        launch.client.recipient_binding(**values)
    assert not launch.server.calls


def test_recipient_binding_refuses_ambiguous_or_stale_durable_records(
    launch: OwnedLaunch,
) -> None:
    duplicate = {**launch.record, "token": "b" * 64}
    duplicate_path = launch.client.binding_path(duplicate["token"])
    duplicate_path.parent.mkdir(parents=True)
    duplicate_path.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(RuntimeError, match="exactly one"):
        launch.client.recipient_binding(
            launch.record["recipient"]["thread_id"],
            launch.record["recipient"]["cwd"],
            launch.record["profile"],
        )
    duplicate_path.unlink()
    stale = duplicate_path.parent.parent / "stale" / "client.json"
    stale.parent.mkdir()
    stale.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(RuntimeError, match="exactly one"):
        launch.client.recipient_binding(
            launch.record["recipient"]["thread_id"],
            launch.record["recipient"]["cwd"],
            launch.record["profile"],
        )
    assert not launch.server.calls


def test_recipient_binding_ignores_malformed_unrelated_records(
    launch: OwnedLaunch,
) -> None:
    malformed = launch.client.binding_path("c" * 64)
    malformed.parent.mkdir(parents=True)
    malformed.write_text("{", encoding="utf-8")
    assert launch.client.recipient_binding(
        launch.record["recipient"]["thread_id"],
        launch.record["recipient"]["cwd"],
        launch.record["profile"],
    ) == launch.client.freeze_binding(launch.record)


@pytest.mark.parametrize(
    "arguments", [["--no-daemon", "prompt"], ["--remote", "unix:///external", "prompt"]]
)
def test_explicit_external_backend_never_starts_or_claims_a_managed_daemon(
    launch: OwnedLaunch, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    def unexpected(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("explicit external backend started a managed daemon")

    monkeypatch.setattr(launch.client.subprocess, "run", unexpected)
    prepared, _environment, ticket = launch.client.prepare(
        arguments,
        launch.server.directory,
        {"CODEX_HOME": launch.record["profile"]},
        executable="native",
    )
    assert ticket["schema_version"] == 1
    assert ticket["backend"] is None
    assert prepared[-len(arguments) :] == arguments
    launch.client.release(ticket)


def test_owned_backend_selection_excludes_administrative_and_external_commands(
    launch: OwnedLaunch,
) -> None:
    sessions = __import__("codex.sessions", fromlist=["requires_verified_handoff"])
    assert sessions.requires_verified_handoff(True, ["prompt"])
    for arguments in (
        ["queue"],
        ["--help"],
        ["--no-daemon", "prompt"],
        ["--remote", "unix:///external", "prompt"],
    ):
        assert not sessions.requires_verified_handoff(True, arguments)


@pytest.mark.parametrize(
    ("requested", "arguments", "expected"),
    [
        (None, [], True),
        (None, ["--no-daemon"], False),
        (None, ["--remote", "unix:///external"], False),
        (False, [], False),
    ],
)
def test_closed_codex_recovery_selects_a_managed_backend_only_for_local_mode(
    requested: bool | None,
    arguments: list[str],
    expected: bool,
) -> None:
    sessions = __import__("codex.sessions", fromlist=["recovery_owns_backend"])
    assert (
        sessions.recovery_owns_backend({"status": "closed"}, requested, arguments)
        is expected
    )


@pytest.mark.parametrize(
    ("item", "arguments"),
    [
        ({"status": "unknown"}, []),
        ({"status": "closed"}, ["--no-daemon"]),
        ({"status": "closed"}, ["--remote=unix:///external"]),
    ],
)
def test_required_managed_recovery_rejects_nonlocal_or_unclosed_targets(
    item: dict[str, Any],
    arguments: list[str],
) -> None:
    sessions = __import__("codex.sessions", fromlist=["recovery_owns_backend"])
    with pytest.raises(ValueError, match="required managed Codex recovery"):
        sessions.recovery_owns_backend(item, True, arguments)


@pytest.mark.parametrize(
    "problem", [None, "missing", "disabled", "untrusted", "loaded"]
)
def test_backend_readiness_requires_hooks_and_an_empty_native_backend(
    launch: OwnedLaunch, problem: str | None
) -> None:
    plugin = Path(launch.record["launch_cwd"]) / "plugin"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / ".codex-plugin/plugin.json").write_text(
        json.dumps(
            {
                "hooks": [
                    {
                        "SessionStart": [
                            {"hooks": [{"type": "command", "command": "fixture-hook"}]}
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    launch.server.loaded = ["thread"] if problem == "loaded" else []
    launch.server.hooks = (
        []
        if problem == "missing"
        else [
            {
                "key": "fixture:startup",
                "pluginId": "agent@fixture",
                "eventName": "sessionStart",
                "matcher": None,
                "handlerType": "command",
                "command": "fixture-hook",
                "enabled": problem != "disabled",
                "trustStatus": "untrusted" if problem == "untrusted" else "trusted",
            }
        ]
    )
    captured = dict(launch.record["backend"])
    if problem is None:
        launch.client.await_backend(launch.record, {"agent@fixture": plugin})
        assert len(launch.record["backend"]["socket_identity"]) == 2
    else:
        with pytest.raises(
            RuntimeError, match=r"Required hooks are not ready|already contains"
        ):
            launch.client.await_backend(launch.record, {"agent@fixture": plugin})
    assert launch.record["backend"] == captured


@pytest.mark.parametrize("identity", ["socket_identity", "parent_identity"])
def test_backend_readiness_refuses_replaced_or_uncaptured_endpoint_before_transport(
    launch: OwnedLaunch, identity: str
) -> None:
    launch.record["backend"].pop(identity)
    with pytest.raises(RuntimeError, match="endpoint was replaced"):
        launch.client.await_backend(launch.record, {})
    assert launch.server.calls == []


def test_native_completion_uses_native_input_and_rechecks_the_frontend_anchor(
    launch: OwnedLaunch,
) -> None:
    rollout = Path(launch.record["profile"]) / "sessions/rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rows = [
        {"type": "session_meta", "payload": {"id": "thread", "source": "cli"}},
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-turn"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "native-turn",
                "item": {"type": "UserMessage", "client_id": NATIVE_MESSAGE},
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "native-turn"},
        },
    ]
    rollout.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    guard = launch.client.NativeCompletion(
        {
            **launch.record,
            "session_id": "thread",
            "native_rollout": str(rollout),
            "native_turn_id": "native-turn",
            "client_message_id": NATIVE_MESSAGE,
            "frontend_message_id": FRONTEND_MESSAGE,
        }
    )
    assert guard.poll()
    recording = Path(launch.record["recording_path"])
    recording.write_text(
        recording.read_text(encoding="utf-8").replace(
            FRONTEND_MESSAGE, "new-frontend-message"
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="frontend input changed"):
        guard.poll()


@pytest.mark.parametrize("damage", ["missing", "malformed", "kind"])
def test_unavailable_owned_frontend_never_opens_shared_transport(
    launch: OwnedLaunch, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    path = Path(launch.client.binding_path(launch.record["token"]))
    if damage == "missing":
        path.unlink()
    elif damage == "malformed":
        path.write_text("{", encoding="utf-8")
    else:
        launch.record["backend"]["kind"] = "unknown"
        launch.save()

    def unexpected(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("An unavailable owned launch selected shared transport")

    monkeypatch.setattr(launch.client, "daemon_identity", unexpected)
    monkeypatch.setattr(launch.client.hook_trust, "AppServer", unexpected)
    with pytest.raises(RuntimeError, match="binding"):
        launch.client.recorded_client(
            "thread",
            launch.table[104],
            launch.table.get,
            launch.table,
            project=launch.server.directory,
            peer_state=launch.peer_state,
        )


def test_missing_owned_backend_never_connects_to_the_shared_daemon(
    launch: OwnedLaunch, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path(launch.client.binding_path(launch.record["token"])).unlink()
    daemon = Path(launch.record["profile"]) / "app-server-daemon/daemon.pid"
    daemon.parent.mkdir(parents=True)
    daemon.write_text(
        json.dumps({"pid": 999, "processStartTime": "other"}), encoding="utf-8"
    )

    def unexpected(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("An unavailable owned launch connected to shared transport")

    monkeypatch.setattr(launch.client.hook_trust, "AppServer", unexpected)
    with pytest.raises(RuntimeError, match="daemon identity"):
        launch.client.recorded_client(
            "thread",
            launch.table[103],
            launch.table.get,
            launch.table,
            project=launch.server.directory,
            peer_state=launch.peer_state,
        )
