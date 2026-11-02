"""Disposable native metadata and real Store state for Agent close tests."""

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/utility/peer/scripts"))
import register_session
import session_close
from codex import client as cli_client
from codex import hook_trust
from host import Host
from tj_cli import processes


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WATCHER = load("test_close_watch", "plugins/agent/.agents/agent/utility/peer/scripts/client_close_watcher.py")
LIFECYCLE = load("test_close_hooks", "plugins/agent/.agents/agent/runtime/close_lifecycle.py")


class CloseFixture:
    def setup_close(self, host="codex"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.host = host
        self.profile = self.root / "profile"
        self.rollout = self.profile / "sessions" / "rollout.jsonl"
        self.rollout.parent.mkdir(parents=True)
        self.patch(mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(self.root),
            "CODEX_HOME": str(self.profile), "CLAUDE_CONFIG_DIR": str(self.profile)}))
        self.spawn = self.patch(mock.patch.object(session_close, "start_agent_watcher", side_effect=self.acknowledge_watcher))
        self.patch(mock.patch.object(LIFECYCLE.secrets, "token_urlsafe", return_value="token"))
        self.stop_patch = mock.patch.object(processes, "stop", side_effect=lambda _identity, *, before_signal: before_signal())
        self.primitive_stop = self.patch(self.stop_patch)
        self.registry = self.root / "cli-sessions/one.json"
        self.target = self.root / "target.json"
        self.target.write_text("terminal acceptance source\n", encoding="utf-8")
        self.transcript = self.root / "one.jsonl"
        self.append({"type": "user", "uuid": "user-one", "message": {"content": "close"}})
        self.backend = {"pid": 10, "process_start_time": "daemon-start"}
        self.infos = {10: register_session.ProcessInfo(10, 1, "codex.exe", 10.0),
                      20: register_session.ProcessInfo(20, 1, host + ".exe", 20.0)}
        self.patch(mock.patch.object(session_close, "process_info", side_effect=self.infos.get))
        self.patch(mock.patch.object(WATCHER.os, "getpid", return_value=20))
        self.patch(mock.patch.object(register_session, "build_process_lookup", return_value=self.infos.get))
        self.patch(mock.patch.object(register_session, "process_table", side_effect=lambda: self.infos))
        self.patch(mock.patch.object(session_close.os, "getppid", return_value=20))
        self.patch(mock.patch.object(session_close.os, "getcwd", return_value=str(self.root)))
        self.patch(mock.patch.object(register_session, "read_host_names", return_value={"codex", "claude"}))
        self.native_input = "input-one"
        self.server = mock.Mock()
        self.server.request.side_effect = self.native_request
        appserver = self.patch(mock.patch.object(hook_trust, "AppServer"))
        appserver.return_value.__enter__.return_value = self.server
        self.patch(mock.patch.object(cli_client, "daemon_identity", return_value=self.backend))
        self.recording = self.root / ("cli-clients/" + "a" * 64 + "/events.jsonl")
        self.recording.parent.mkdir(parents=True)
        self.recording.write_text(json.dumps({"dir": "meta", "kind": "session_start"}) + "\n" +
            json.dumps({"dir": "from_tui", "kind": "op", "payload": {
                "UserTurn": {"client_user_message_id": "input-one"}}}) + "\n", encoding="utf-8")
        self.append_rollout({"type": "session_meta", "payload": {"id": "one", "source": "cli"}})
        self.append_rollout({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-one"}})
        self.append_rollout({"type": "event_msg", "payload": {
            "type": "item_completed", "turn_id": "turn-one",
            "item": {"type": "UserMessage", "client_id": "input-one"},
        }})
        self.write(self.recording.with_name("client.json"), {"token": "a" * 64, "pid": 20,
            "profile": str(self.profile.resolve()), "backend": self.backend, "created_at": 20.0,
            "recorded_at": 21.0, "recording_path": str(self.recording), "launch_cwd": str(self.root)})
        self.commands = {10: ["codex", "app-server"], 20: [host]}
        self.contexts = {20: {"profile": str(self.profile.resolve()), cli_client.RECORD_ENV: "1",
                             cli_client.RECORD_PATH_ENV: str(self.recording)}}
        self.patch(mock.patch.object(cli_client, "process_arguments", side_effect=self.commands.get))
        self.patch(mock.patch.object(cli_client, "process_context", side_effect=lambda pid: self.contexts.get(pid, {})))
        self.store_module = register_session._load_session_store()
        self.store = self.store_module.SessionStore()
        self.addCleanup(self.store.close)
        initial_pid = 10 if host == "codex" else 20
        self.handle = self.store.start_session(host, "one", self.root,
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
        LIFECYCLE.run("prompt", Host(host), {"session_id": "one", "turn_id": "turn-one",
                                            "transcript_path": str(self.transcript)})

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def native_request(self, method, params):
        if method == "thread/read":
            return {"thread": {"source": "cli", "path": str(self.rollout)}}
        if method == "thread/turns/list":
            return {"data": [{"id": "turn-one"}]}
        return {"data": [{"item": {"type": "userMessage", "clientId": self.native_input}}]}

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def append(self, record):
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")

    def append_rollout(self, record):
        with self.rollout.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")

    def receipt(self, state="accepted"):
        source = {"path": str(self.target), "sha256": hashlib.sha256(self.target.read_bytes()).hexdigest()}
        sources = [source]
        owner = {"host": self.host, "session_id": "one"}
        bound_delivery = None
        candidate_id = hashlib.sha256(
            json.dumps(
                {"owner": owner, "sources": sources, "bound_delivery": bound_delivery},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        sources_sha256 = hashlib.sha256(json.dumps(sources, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        audit_result = self.root / "audit-result.json"
        audit_result.write_text("{\"findings\": []}\n", encoding="utf-8")
        path = self.root / "terminal-acceptance.json"
        self.write(path, {"schema_version": 1, "kind": "terminal-acceptance", "candidate_id": candidate_id,
                          "acceptance_generation": candidate_id, "state": state,
                          "owner": owner, "sources": sources, "bound_delivery": bound_delivery,
                          "deterministic": {"blockers": []},
                          "aggregate": {"known": True, "owned": [], "responsibilities": [], "children": [], "waits": []},
                          "audit": {"state": "passed", "candidate_id": candidate_id, "sources_sha256": sources_sha256,
                                    "reservation_id": "reservation-one", "native_child_id": "native-child-one",
                                    "result": {"path": str(audit_result), "sha256": hashlib.sha256(audit_result.read_bytes()).hexdigest()}},
                          "cleanup": {"state": "retained"}, "checked_at": time.time()})
        return path

    def pending_cleanup_receipt(self, *, worker=True, mismatch=None):
        path = self.receipt()
        obligation_path = self.root / "cleanup-obligation.json"
        worker_path = self.root / "cleanup-worker.json"
        owner = {"host": self.host, "session_id": "one"}
        if mismatch == "owner":
            worker_owner = {"host": self.host, "session_id": "other"}
        else:
            worker_owner = owner
        worktree = self.root / "cleanup-worktree" if mismatch == "worktree" else self.root
        obligation = {"schema_version": 1, "kind": "targeted-cleanup", "obligation_id": "cleanup-one",
                      "state": "pending", "owner": owner, "worktree": str(self.root), "primary": True,
                      "branch": "main", "head": "head", "pr": None, "repository": "repo", "holders": [],
                      "identity_sha256": "b" * 64, "launch_token": "launch-one", "launch": None,
                      "worker": None, "created_at": 1.0, "updated_at": 1.0, "reason": "closeout"}
        self.write(obligation_path, obligation)
        if worker:
            self.write(worker_path, {**obligation, "state": "running", "owner": worker_owner,
                                     "worktree": str(worktree), "launch": {"token": "launch-one",
                                     "launcher": {"pid": 99, "started_at": 99.0}},
                                     "worker": {"pid": 20, "started_at": 20.0}})
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt["cleanup"] = {"state": "pending", "obligation_id": "cleanup-one",
                               "obligation_path": str(obligation_path), "receipt_path": str(worker_path),
                               "owner": owner, "worktree": str(self.root)}
        self.write(path, receipt)
        return path

    def retired_goal_receipt(self):
        path = self.receipt()
        checkpoint = self.root / "checkpoint-goal.json"
        checkpoint.write_bytes(self.target.read_bytes())
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt["checkpoint"] = {"retired_goal": True, "sources": [
            {"path": str(checkpoint), "sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest()}
        ]}
        self.write(path, receipt)
        self.target.unlink()
        return path, checkpoint

    def acknowledge_watcher(self, _script, arguments):
        request = Path(arguments[1])
        digest = arguments[3]
        value = json.loads(request.read_text(encoding="utf-8"))
        self.write(Path(value["acknowledgement"]), {"request_sha256": digest, "pid": 20, "started_at": 20.0})
        return 99

    def arm(self):
        prepared = LIFECYCLE.prepare(Host(self.host), "one", self.profile, "task-completion", session_close, self.receipt())
        LIFECYCLE.arm(Host(self.host), "one", self.profile, prepared["release_generation"], session_close)
        self.request_path = self.root / "terminal-release/requests/token.json"
        self.digest = hashlib.sha256(self.request_path.read_bytes()).hexdigest()
        self.request = WATCHER.load_request(self.request_path, self.digest)

    def stop(self):
        if self.host == "claude":
            self.append({"type": "assistant", "message": {"stop_reason": "end_turn"}})
        self.store.record_stop(self.handle, self.store_module.Checkout.probe(self.root))
        if self.host == "codex":
            self.append_rollout({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-one"}})
        LIFECYCLE.run("stop", Host(self.host), {"session_id": "one", "turn_id": "turn-one",
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
        return self.store.get_session(self.handle)

    def outcome(self):
        return json.loads(Path(self.request["result"]).read_text(encoding="utf-8"))
