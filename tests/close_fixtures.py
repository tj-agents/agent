"""Disposable native metadata and real Store state for Agent close tests."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agents/agent/runtime"))
sys.path.insert(0, str(ROOT / ".agents/agent/utility/peer/scripts"))
import cli_client
import codex_hook_trust
import session_close
import register_session


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLOSE = load("test_close_command", ".agents/agent/utility/close/scripts/agent_close.py")
WATCHER = load("test_close_watch", ".agents/agent/utility/peer/scripts/client_close_watcher.py")
LIFECYCLE = load("test_close_hooks", ".agents/agent/runtime/close_lifecycle.py")


class CloseFixture:
    def setup_close(self, host="codex"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.host = host
        self.profile = self.root / "profile"
        self.patch(mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(self.root),
            "CODEX_HOME": str(self.profile), "CLAUDE_CONFIG_DIR": str(self.profile)}))
        self.patch(mock.patch.object(CLOSE, "_session_close", return_value=session_close))
        self.spawn = self.patch(mock.patch.object(session_close, "start_agent_watcher", return_value=99))
        self.cli = self.root / "CLI capability ü"
        (self.cli / ".git").mkdir(parents=True)
        scripts = self.cli / ".agents/cli/utility/close/scripts"
        scripts.mkdir(parents=True)
        self.capability = scripts / "close.py"
        self.capability.write_text("# disposable capability\n", encoding="utf-8")
        (scripts / "process_identity.py").write_text(
            "def stop(identity, *, before_signal):\n    before_signal()\n", encoding="utf-8")
        self.registry = self.root / "cli-sessions/one.json"
        self.target = self.root / "target.json"
        self.transcript = self.root / "one.jsonl"
        self.append({"type": "user", "uuid": "user-one", "message": {"content": "close"}})
        self.backend = {"pid": 10, "process_start_time": "daemon-start"}
        self.infos = {10: register_session.ProcessInfo(10, 1, "codex.exe", 10.0),
                      20: register_session.ProcessInfo(20, 1, host + ".exe", 20.0)}
        self.patch(mock.patch.object(session_close, "process_info", side_effect=self.infos.get))
        self.patch(mock.patch.object(register_session, "build_process_lookup", return_value=self.infos.get))
        self.patch(mock.patch.object(register_session, "process_table", side_effect=lambda: self.infos))
        self.patch(mock.patch.object(session_close.os, "getppid", return_value=10 if host == "codex" else 20))
        self.patch(mock.patch.object(session_close.os, "getcwd", return_value=str(self.root)))
        self.patch(mock.patch.object(register_session, "read_host_names", return_value={"codex", "claude"}))
        self.patch(mock.patch.object(session_close, "client_runtime", return_value=cli_client))
        self.native_input = "input-one"
        self.server = mock.Mock()
        self.server.request.side_effect = self.native_request
        appserver = self.patch(mock.patch.object(codex_hook_trust, "AppServer"))
        appserver.return_value.__enter__.return_value = self.server
        self.patch(mock.patch.object(cli_client, "daemon_identity", return_value=self.backend))
        self.recording = self.root / ("cli-clients/" + "a" * 64 + "/events.jsonl")
        self.recording.parent.mkdir(parents=True)
        self.recording.write_text(json.dumps({"dir": "meta", "kind": "session_start"}) + "\n" +
            json.dumps({"dir": "from_tui", "kind": "op", "payload": {
                "UserTurn": {"client_user_message_id": "input-one"}}}) + "\n", encoding="utf-8")
        self.write(self.recording.with_name("client.json"), {"token": "a" * 64, "pid": 20,
            "profile": str(self.profile.resolve()), "backend": self.backend, "created_at": 20.0,
            "recorded_at": 21.0, "recording_path": str(self.recording), "launch_cwd": str(self.root)})
        self.commands = {10: ["codex", "app-server"], 20: [host]}
        self.contexts = {20: {"profile": str(self.profile.resolve()), cli_client.RECORD_ENV: "1",
                             cli_client.RECORD_PATH_ENV: str(self.recording)}}
        self.patch(mock.patch.object(cli_client, "process_arguments", side_effect=self.commands.get))
        self.patch(mock.patch.object(cli_client, "process_context", side_effect=lambda pid: self.contexts.get(pid, {})))
        self.store = register_session._load_session_store()
        self.connection = self.store.connect()
        self.addCleanup(self.connection.close)
        initial_pid = 10 if host == "codex" else 20
        self.handle = self.store.start_session(self.connection, host, "one", self.root,
                                               pid=initial_pid, pid_started_at=float(initial_pid))
        entry = {"host": host, "session_id": "one", "cwd": str(self.root), "pid": initial_pid,
                 "pid_started_at": float(initial_pid), "started_at": 30.0,
                 "profile": register_session.profile_identity(host)}
        if host == "claude":
            frontend = {"pid": 20, "started_at": 20.0}
            terminal = {"kind": "tmux", "binding": {"pid": 1, "started_at": 1.0}}
            receipt = self.root / "recovery-launches/claude.json"
            self.write(receipt, {"session": "one", "profile": entry["profile"], "state": "launched",
                "wrapper": {"child": frontend, "terminal": terminal}})
            entry.update({"frontend": frontend, "terminal": terminal,
                          "provenance": {"source": "agent-startup-binding", "receipt": str(receipt)}})
        self.write(self.registry, entry)

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def native_request(self, method, params):
        if method == "thread/read":
            return {"thread": {"source": "cli"}}
        if method == "thread/turns/list":
            return {"data": [{"id": "turn-one"}]}
        return {"data": [{"item": {"type": "userMessage", "clientId": self.native_input}}]}

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def append(self, record):
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")

    def prepare(self):
        return CLOSE.main(["prepare", "--session-id", "one", "--profile", str(self.profile),
                           "--target", str(self.target), "--transcript", str(self.transcript)])

    def arm_args(self, token="token"):
        return ["arm", "--host", self.host, "--session-id", "one", "--turn-id", "turn-one",
            "--profile", str(self.profile), "--target", str(self.target), "--cli-close", str(self.capability),
            "--completion", str(self.root / "complete.json"), "--cancel", str(self.root / "cancel.json"),
            "--completion-token", token]

    def arm(self):
        self.assertEqual(0, self.prepare())
        self.assertEqual(0, CLOSE.main(self.arm_args()))
        self.request_path = self.root / "client-close/token.json"
        self.digest = hashlib.sha256(self.request_path.read_bytes()).hexdigest()
        self.request = WATCHER.load_request(self.request_path, self.digest)

    def stop(self):
        if self.host == "claude":
            self.append({"type": "assistant", "message": {"stop_reason": "end_turn"}})
        self.store.record_stop(self.connection, self.handle, self.root)
        LIFECYCLE.run("stop", self.host, {"session_id": "one", "turn_id": "turn-one",
                                           "transcript_path": str(self.transcript)})

    def wait(self, during=None):
        elapsed = [0.0]
        def sleep(seconds):
            elapsed[0] += seconds
            if during:
                during(elapsed[0])
        return WATCHER.wait(self.request, self.request_path, self.digest, timeout=0.5, idle=0.1,
                            clock=lambda: elapsed[0], sleep=sleep)

    def row(self):
        return self.store.get_session(self.connection, self.handle)

    def outcome(self):
        return json.loads(Path(self.request["result"]).read_text(encoding="utf-8"))
