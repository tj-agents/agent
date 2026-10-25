"""Real Store lifecycle and late native changes cannot authorize an old close."""

import json
from pathlib import Path
import unittest
from unittest import mock

from close_fixtures import CloseFixture, WATCHER, LIFECYCLE, session_close, cli_client


class ClientCloseWatcherTests(CloseFixture, unittest.TestCase):
    def setUp(self):
        self.setup_close()

    def test_working_arm_stop_settles_then_closes_and_records_end(self):
        self.arm()
        self.stop()
        self.assertEqual(0, self.wait())
        self.assertEqual("closed", self.outcome()["status"])
        self.assertEqual("closed", self.row()["end_reason"])
        self.assertFalse(WATCHER.canonical_path(self.request).exists())
        self.assertGreaterEqual(self.server.request.call_count, 9)

    def test_stop_between_activity_and_completion_reads_preserves_close(self):
        self.arm()
        original_complete = WATCHER.complete
        completed = []
        def stop_before_completion_read(request):
            if not completed:
                completed.append(True)
                self.stop()
            return original_complete(request)
        with mock.patch.object(WATCHER, "complete", side_effect=stop_before_completion_read):
            self.assertEqual(0, self.wait())
        self.assertEqual("closed", self.outcome()["status"])
        self.assertEqual("closed", self.row()["end_reason"])

    def test_stop_store_transition_waits_for_completion_publication(self):
        self.arm()
        self.store.record_stop(self.connection, self.handle, self.root)
        published = []
        def publish_after_wait(_elapsed):
            if not published:
                self.assertFalse(WATCHER.complete(self.request))
                self.assertIsNone(self.row()["ended_at"])
                published.append(True)
                LIFECYCLE.run("stop", "codex", {"session_id": "one", "turn_id": "turn-one"})
        self.assertEqual(0, self.wait(publish_after_wait))
        self.assertEqual([True], published)
        self.assertEqual("closed", self.outcome()["status"])
        self.assertEqual("closed", self.row()["end_reason"])

    def test_no_matching_stop_never_signals(self):
        self.arm()
        with mock.patch.object(session_close, "stop_cli_client") as stop:
            self.assertEqual(2, self.wait())
        stop.assert_not_called()
        self.assertIsNone(self.row()["ended_at"])

    def test_private_codex_uses_the_frozen_frontend_and_native_input(self):
        self.commands[20] = ["codex", "--no-daemon"]
        ticket = json.loads(self.recording.with_name("client.json").read_text(encoding="utf-8"))
        self.write(self.recording.with_name("client.json"), {**ticket, "backend": None})
        with mock.patch.object(session_close.os, "getppid", return_value=20):
            self.arm()
        self.stop()
        self.assertEqual(0, self.wait())
        self.assertIsNone(self.request["entry"]["backend"])
        from close_fixtures import codex_hook_trust
        self.assertFalse(codex_hook_trust.AppServer.call_args.kwargs["existing"])
        self.assertEqual(self.root, codex_hook_trust.AppServer.call_args.args[1])

    def test_canonical_state_mutation_inside_native_rpc_prevents_signal(self):
        self.arm()
        self.stop()
        native = self.native_request
        def request(method, params):
            if method == "thread/items/list":
                self.write(WATCHER.canonical_path(self.request), {**self.request, "mode": "closeout"})
            return native(method, params)
        self.server.request.side_effect = request
        self.assertEqual(0, self.wait())
        self.assertEqual("cancelled", self.outcome()["status"])
        self.assertIsNone(self.row()["ended_at"])

    def test_later_prompt_and_stop_during_settlement_cancel(self):
        self.arm()
        self.stop()
        fired = []
        def changed(_elapsed):
            if not fired:
                fired.append(True)
                self.store.record_prompt(self.connection, self.handle)
                self.store.record_stop(self.connection, self.handle, self.root)
        with mock.patch.object(session_close, "stop_cli_client") as stop:
            self.assertEqual(0, self.wait(changed))
        stop.assert_not_called()
        self.assertEqual("cancelled", self.outcome()["status"])

    def test_newer_native_input_with_unchanged_local_recording_refuses_inside_handle(self):
        self.arm()
        self.stop()
        original = self.recording.read_bytes()
        calls = []
        class Primitive:
            def stop(_self, identity, *, before_signal):
                self.native_input = "other-frontend-input"
                calls.append("held")
                before_signal()
                calls.append("signal")
        with mock.patch.object(session_close, "process_identity_runtime", return_value=Primitive()):
            self.assertEqual(2, self.wait())
        self.assertEqual(["held"], calls)
        self.assertEqual(original, self.recording.read_bytes())
        self.assertIsNone(self.row()["ended_at"])

    def test_local_generation_cancel_and_store_are_checked_after_native_rpc(self):
        self.arm()
        self.stop()
        native = self.native_request
        def request(method, params):
            if method == "thread/items/list":
                self.store.record_prompt(self.connection, self.handle)
                self.store.record_stop(self.connection, self.handle, self.root)
            return native(method, params)
        self.server.request.side_effect = request
        self.assertEqual(2, self.wait())
        self.assertIsNone(self.row()["ended_at"])

    def test_mutated_request_and_replaced_generation_never_signal(self):
        self.arm()
        self.stop()
        def mutate(_elapsed):
            self.write(WATCHER.canonical_path(self.request), {**self.request, "completion_token": "new"})
        with mock.patch.object(session_close, "stop_cli_client") as stop:
            self.assertEqual(0, self.wait(mutate))
        stop.assert_not_called()
        self.assertEqual("new", json.loads(WATCHER.canonical_path(self.request).read_text(encoding="utf-8"))["completion_token"])
        self.write(self.request_path, {**self.request, "mode": "closeout"})
        with self.assertRaisesRegex(session_close.Refusal, "frozen close request changed"):
            WATCHER.load_request(self.request_path, self.digest)

    def test_prompt_end_or_second_stop_keeps_cancellation_routing_until_outcome(self):
        self.arm()
        self.stop()
        LIFECYCLE.run("stop", "codex", {"session_id": "one", "turn_id": "turn-one"})
        self.assertTrue(WATCHER.canonical_path(self.request).exists())
        self.assertEqual(0, self.wait())
        self.assertEqual("cancelled", self.outcome()["status"])
        self.assertIsNone(self.row()["ended_at"])


class ClaudeWatcherTests(CloseFixture, unittest.TestCase):
    def setUp(self):
        self.setup_close("claude")

    def test_full_claude_lifecycle_records_end_only_after_exact_exit(self):
        self.arm()
        self.stop()
        self.assertEqual(0, self.wait())
        self.assertEqual("closed", self.outcome()["status"])
        self.assertEqual("closed", self.row()["end_reason"])

    def test_user_changes_inside_held_handle_preserve_claude(self):
        self.arm()
        self.stop()
        signals = []
        class Primitive:
            def stop(_self, identity, *, before_signal):
                self.append({"type": "user", "uuid": "later", "message": {"content": "continue"}})
                before_signal()
                signals.append(True)
        with mock.patch.object(session_close, "process_identity_runtime", return_value=Primitive()):
            self.assertEqual(0, self.wait())
        self.assertEqual([], signals)
        self.assertIsNone(self.row()["ended_at"])


if __name__ == "__main__":
    unittest.main()
