"""Managed Codex frontend attribution and Agent-side attachment tests."""

import errno
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agents/agent/runtime"))
import codex_hook_trust


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CLIENT = load("agent_cli_client_tests", ROOT / ".agents/agent/runtime/cli_client.py")
sys.path.insert(0, str(ROOT / ".agents/agent/utility/peer/scripts"))
import session_close
import register_session


def events(identity):
    return [
        {"dir": "meta", "kind": "session_start"},
        {"dir": "from_tui", "kind": "op", "payload": {"UserTurn": {"client_user_message_id": identity}}},
    ]


class ClientAttributionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.profile = self.root / "profile"
        self.backend = {"pid": 1, "process_start_time": "native-start"}
        self.infos = {}
        self.contexts = {}
        self.commands = {1: ["codex", "app-server"]}
        self.patchers = [
            mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(self.root), "CODEX_HOME": str(self.profile)}),
            mock.patch.object(CLIENT.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="")),
            mock.patch.object(CLIENT, "daemon_identity", return_value=self.backend),
            mock.patch.object(CLIENT, "process_context", side_effect=lambda pid: self.contexts.get(pid, {})),
            mock.patch.object(CLIENT, "process_arguments", side_effect=lambda pid: self.commands[pid]),
            mock.patch.object(CLIENT, "current_input", return_value={"turn_id": "turn", "client_message_id": "own-input"}),
            mock.patch.object(codex_hook_trust, "AppServer"),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def bind(self, pid=123, identity="own-input"):
        prepared, environment, ticket = CLIENT.prepare("codex", ["resume", "thread"], self.root, os.environ,
                                                        executable="codex")
        CLIENT.attach(ticket, pid)
        Path(ticket["recording_path"]).write_text(
            "".join(json.dumps(value) + "\n" for value in events(identity)), encoding="utf-8",
        )
        self.infos[pid] = SimpleNamespace(pid=pid, name="codex.exe", started_at=ticket["created_at"])
        self.contexts[pid] = {"profile": str(self.profile.resolve()), CLIENT.RECORD_ENV: "1",
                              CLIENT.RECORD_PATH_ENV: ticket["recording_path"]}
        self.commands[pid] = ["codex", *prepared]
        return ticket, environment

    def resolve(self):
        nearest = self.infos[123]
        return CLIENT.recorded_client("thread", nearest, self.infos.get, self.infos)

    def test_recorder_sanitizes_daemon_and_selects_only_matching_frontend(self):
        ticket, environment = self.bind()
        daemon_environment = CLIENT.subprocess.run.call_args.kwargs["env"]
        self.assertNotIn(CLIENT.RECORD_ENV, daemon_environment)
        self.assertEqual("1", environment[CLIENT.RECORD_ENV])
        info, record = self.resolve()
        self.assertEqual(123, info.pid)
        self.assertEqual("own-input", record["client_message_id"])
        self.assertEqual("turn", record["turn_id"])
        CLIENT.release(ticket)

    def test_shared_daemon_and_frontend_are_verified_as_distinct_bindings(self):
        self.bind()
        CLIENT.daemon_identity.reset_mock()
        self.assertEqual(123, self.resolve()[0].pid)
        CLIENT.daemon_identity.assert_called_once_with()
        self.assertEqual(123, CLIENT.recorded_client(
            "thread", SimpleNamespace(pid=1, name="codex.exe"), self.infos.get, self.infos)[0].pid)

    def test_shared_backend_ancestor_selects_only_its_verified_frontend(self):
        self.bind()
        backend = SimpleNamespace(pid=1, name="codex.exe", started_at=9.0)
        self.infos[1] = backend
        self.contexts[1] = {"profile": str(self.profile.resolve())}
        self.assertEqual(123, CLIENT.recorded_client("thread", backend, self.infos.get, self.infos)[0].pid)

        CLIENT.daemon_identity.return_value = {"pid": 1, "process_start_time": "reused-daemon"}
        with self.assertRaisesRegex(RuntimeError, "Cannot attribute"):
            CLIENT.recorded_client("thread", backend, self.infos.get, self.infos)

        CLIENT.daemon_identity.side_effect = RuntimeError("Native Codex daemon metadata could not be verified")
        with self.assertRaisesRegex(RuntimeError, "daemon metadata"):
            CLIENT.recorded_client("thread", backend, self.infos.get, self.infos)

        CLIENT.daemon_identity.side_effect = None
        CLIENT.daemon_identity.return_value = self.backend
        self.commands[999] = ["codex", "app-server"]
        with self.assertRaisesRegex(RuntimeError, "Cannot attribute"):
            CLIENT.recorded_client("thread", SimpleNamespace(pid=999, name="codex.exe", started_at=9.0),
                                   self.infos.get, self.infos)

    def test_revalidation_rechecks_the_bound_frontend_not_the_shared_daemon(self):
        frontend = SimpleNamespace(pid=123, name="codex.exe", started_at=10.0)
        entry = {"host": "codex", "session_id": "thread", "pid": 123, "backend": self.backend,
                 "client_identity": "managed-input", "cwd": str(self.root)}
        with mock.patch.object(session_close, "process_info", return_value=frontend), \
                mock.patch.object(session_close, "current_codex_client", return_value=(frontend, {})) as current:
            with self.assertRaisesRegex(session_close.Refusal, "binding changed"):
                session_close.revalidate_codex_client(entry)
        self.assertEqual(123, current.call_args.args[1].pid)

    def test_newer_unattributed_input_refuses_the_old_frontend(self):
        ticket, _environment = self.bind()
        with Path(ticket["recording_path"]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(events("new-input")[1]) + "\n")
        with self.assertRaisesRegex(RuntimeError, "Cannot attribute"):
            self.resolve()

    def test_nearest_frontend_selects_its_own_shared_daemon_recording(self):
        first, _environment = self.bind()
        self.bind(pid=456)
        self.assertEqual(123, self.resolve()[0].pid)
        CLIENT.release(first)

    def test_disappeared_peer_does_not_discard_a_verified_frontend(self):
        self.bind()
        peer = SimpleNamespace(pid=456, name="codex.exe", started_at=11.0)
        self.infos[456] = peer

        for error in (FileNotFoundError(), OSError(errno.ESRCH, "process exited")):
            with self.subTest(error=type(error).__name__):
                table = dict(self.infos)

                def context(pid):
                    if pid == 456:
                        self.infos.pop(456, None)
                        raise error
                    return self.contexts.get(pid, {})

                with mock.patch.object(CLIENT, "process_context", side_effect=context):
                    self.assertEqual(123, CLIENT.recorded_client(
                        "thread", self.infos[123], self.infos.get, table)[0].pid)
                self.infos[456] = peer

    def test_live_uninspectable_peer_refuses_the_frontend(self):
        self.bind()
        self.infos[456] = SimpleNamespace(pid=456, name="codex.exe", started_at=11.0)

        def context(pid):
            if pid == 456:
                raise PermissionError("permission denied")
            return self.contexts.get(pid, {})

        with mock.patch.object(CLIENT, "process_context", side_effect=context), \
                self.assertRaisesRegex(RuntimeError, "Cannot attribute"):
            CLIENT.recorded_client("thread", self.infos[123], self.infos.get, dict(self.infos))

    def test_unknown_peer_state_does_not_mean_the_peer_exited(self):
        self.bind()
        peer = SimpleNamespace(pid=456, name="codex.exe", started_at=11.0)
        self.infos[456] = peer

        def context(pid):
            if pid == 456:
                raise FileNotFoundError()
            return self.contexts.get(pid, {})

        for state in (("unknown", None), ("present", peer), ("present", None)):
            with self.subTest(state=state[0], current=state[1] is not None), \
                    mock.patch.object(CLIENT, "process_context", side_effect=context), \
                    self.assertRaisesRegex(RuntimeError, "Cannot attribute"):
                CLIENT.recorded_client("thread", self.infos[123], self.infos.get, dict(self.infos),
                                       peer_state=lambda _pid, result=state: result)

    def test_absent_or_reused_peer_state_skips_a_disappeared_peer(self):
        self.bind()
        peer = SimpleNamespace(pid=456, name="codex.exe", started_at=11.0)
        self.infos[456] = peer

        def context(pid):
            if pid == 456:
                raise FileNotFoundError()
            return self.contexts.get(pid, {})

        for state in (("absent", None), ("present", SimpleNamespace(pid=456, started_at=12.0))):
            with self.subTest(state=state[0]):
                with mock.patch.object(CLIENT, "process_context", side_effect=context):
                    self.assertEqual(123, CLIENT.recorded_client(
                        "thread", self.infos[123], self.infos.get, dict(self.infos),
                        peer_state=lambda _pid, result=state: result)[0].pid)

    def test_canonical_caller_passes_the_fresh_peer_state(self):
        runtime = mock.Mock()
        runtime.recorded_client.return_value = (SimpleNamespace(pid=123), {
            "launch_cwd": str(self.root), "client_message_id": "input", "turn_id": "turn",
            "recording_path": "events.jsonl", "backend": self.backend, "profile": str(self.profile),
        })
        nearest = SimpleNamespace(pid=123)
        table = {123: nearest}
        with mock.patch.object(session_close, "client_runtime", return_value=runtime), \
                mock.patch.object(session_close.register_session, "process_table", return_value=table):
            session_close.current_codex_client("thread", nearest)
        self.assertIs(session_close.process_state, runtime.recorded_client.call_args.kwargs["peer_state"])

    def test_incomplete_or_duplicate_recording_refuses(self):
        ticket, _environment = self.bind()
        path = Path(ticket["recording_path"])
        path.write_text(json.dumps(events("own-input")[0]), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "incomplete or oversized"):
            self.resolve()
        path.write_text("".join(json.dumps(value) + "\n" for value in [*events("own-input"), *events("other")]),
                        encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "no unique startup header"):
            self.resolve()

    def test_new_input_in_final_guard_sends_no_signal(self):
        entry = {
            "host": "codex", "pid": 123, "pid_started_at": 10.0,
            "client_identity": "managed-input", "recording_path": "events.jsonl",
            "client_message_id": "old-input", "profile": str(self.profile), "backend": self.backend,
        }
        stopped = []

        class Primitive:
            @staticmethod
            def stop(_identity, *, before_signal):
                before_signal()
                stopped.append(True)

        with mock.patch.object(session_close, "process_info", return_value=SimpleNamespace(name="codex.exe")), \
                mock.patch.object(session_close, "verified_live", return_value=True), \
                mock.patch.object(session_close, "revalidate_codex_client"), \
                mock.patch.object(session_close, "client_runtime", return_value=CLIENT), \
                mock.patch.object(session_close, "process_identity_runtime", return_value=Primitive), \
                mock.patch.object(CLIENT, "process_arguments", return_value=["codex"]), \
                mock.patch.object(CLIENT, "process_context", return_value={
                    "profile": str(self.profile), CLIENT.RECORD_ENV: "1", CLIENT.RECORD_PATH_ENV: "events.jsonl",
                }), \
                mock.patch.object(CLIENT, "daemon_identity", return_value=self.backend), \
                mock.patch.object(CLIENT, "latest_input", return_value="new-input"):
            with self.assertRaisesRegex(session_close.Refusal, "binding changed"):
                session_close.stop_cli_client(entry)
        self.assertEqual([], stopped)


class SessionStoreAttachmentTests(unittest.TestCase):
    def test_frontend_attachment_preserves_turn_state_siblings_and_end_state(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": directory}):
            store = register_session._load_session_store()
            connection = store.connect()
            try:
                own = store.start_session(connection, "codex", "own", directory, pid=10, pid_started_at=1.0)
                peer = store.start_session(connection, "codex", "peer", directory, pid=10, pid_started_at=1.0)
                store.record_stop(connection, own, directory)
                before = store.get_session(connection, own)
                store.attach_session_client(connection, own, 20, 2.0, directory)
                after = store.get_session(connection, own)
                self.assertEqual("awaiting_input", after["turn_state"])
                self.assertEqual(before["last_activity_at"], after["last_activity_at"])
                self.assertEqual(20, after["pid"])
                self.assertEqual(10, store.get_session(connection, peer)["pid"])
                store.end_session(connection, own, "closed")
                store.attach_session_client(connection, own, 30, 3.0, directory)
                self.assertEqual(20, store.get_session(connection, own)["pid"])
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
