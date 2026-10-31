"""Real Store lifecycle and late native changes cannot authorize an old close."""

import json
from pathlib import Path
import unittest
from unittest import mock

from close_fixtures import CloseFixture, Host, WATCHER, LIFECYCLE, session_close, cli_client, processes


class ClientCloseWatcherTests(CloseFixture, unittest.TestCase):
    def setUp(self):
        self.setup_close()

    def test_working_arm_stop_settles_then_closes_and_records_end(self):
        self.arm()
        self.stop()
        self.assertEqual(0, self.wait())
        self.assertEqual("closed", self.outcome()["status"])
        self.assertEqual("closed", self.row().end_reason)
        self.assertFalse(WATCHER.canonical_path(self.request).exists())
        self.assertGreaterEqual(self.server.request.call_count, 9)

    def test_native_completion_closes_without_a_store_stop(self):
        self.arm()
        self.append_rollout({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-one"}})
        self.assertEqual(0, self.wait())
        self.assertEqual("closed", self.outcome()["status"])
        self.assertEqual("closed", self.row().end_reason)

    def test_store_bookkeeping_failure_after_a_real_stop_keeps_the_close_proof(self):
        self.arm()
        self.stop()
        with mock.patch.object(WATCHER, "_record_end", side_effect=OSError("Store unavailable")):
            self.assertEqual(0, self.wait())
        outcome = self.outcome()
        self.assertEqual({"status": "closed", "session_id": "one"},
                         {key: outcome[key] for key in ("status", "session_id")})
        self.assertEqual("session Store bookkeeping failed after close: Store unavailable", outcome["reason"])
        self.assertIsNone(self.row().ended_at)

    def test_refused_stop_does_not_attempt_store_bookkeeping(self):
        self.arm()
        self.stop()
        with mock.patch.object(session_close, "stop_cli_client", side_effect=session_close.Refusal("still live")), \
                mock.patch.object(WATCHER, "_record_end") as record_end:
            self.assertEqual(2, self.wait())
        record_end.assert_not_called()
        self.assertEqual("refused", self.outcome()["status"])

    def test_new_native_turn_at_the_final_signal_guard_cancels(self):
        self.arm()
        self.stop()
        calls = []

        class Primitive:
            def stop(_self, _identity, *, before_signal):
                self.append_rollout({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "later"}})
                before_signal()
                calls.append("signal")

        with mock.patch.object(processes, "stop", side_effect=Primitive().stop):
            self.assertEqual(0, self.wait())
        self.assertEqual([], calls)
        self.assertEqual("cancelled", self.outcome()["status"])

    def test_stop_between_activity_and_completion_reads_preserves_close(self):
        self.arm()
        self.stop()
        self.assertEqual(0, self.wait())
        self.assertEqual("closed", self.outcome()["status"])
        self.assertEqual("closed", self.row().end_reason)

    def test_stop_store_transition_waits_for_completion_publication(self):
        self.arm()
        self.stop()
        self.assertEqual(0, self.wait())
        self.assertEqual("closed", self.outcome()["status"])
        self.assertEqual("closed", self.row().end_reason)

    def test_no_matching_stop_never_signals(self):
        self.arm()
        with mock.patch.object(session_close, "stop_cli_client") as stop:
            self.assertEqual(2, self.wait())
        stop.assert_not_called()
        self.assertIsNone(self.row().ended_at)

    def test_private_codex_uses_the_frozen_frontend_and_native_input(self):
        self.commands[20] = ["codex", "--no-daemon"]
        ticket = json.loads(self.recording.with_name("client.json").read_text(encoding="utf-8"))
        self.write(self.recording.with_name("client.json"), {**ticket, "backend": None})
        with mock.patch.object(session_close.os, "getppid", return_value=20):
            self.arm()
        self.stop()
        self.assertEqual(0, self.wait())
        self.assertIsNone(self.request["entry"]["backend"])
        from close_fixtures import hook_trust
        self.assertFalse(hook_trust.AppServer.call_args.kwargs["existing"])
        self.assertEqual(self.root.resolve(), hook_trust.AppServer.call_args.args[1].resolve())

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
        self.assertIsNone(self.row().ended_at)

    def test_later_prompt_and_stop_during_settlement_cancel(self):
        self.arm()
        self.stop()
        fired = []
        def changed(_elapsed):
            if not fired:
                fired.append(True)
                self.append_rollout({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "later"}})
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
        with mock.patch.object(processes, "stop", side_effect=Primitive().stop):
            self.assertEqual(2, self.wait())
        self.assertEqual(["held"], calls)
        self.assertEqual(original, self.recording.read_bytes())
        self.assertIsNone(self.row().ended_at)

    def test_store_transition_does_not_replace_the_native_completion_guard(self):
        self.arm()
        self.stop()
        native = self.native_request
        def request(method, params):
            if method == "thread/items/list":
                self.store.update_session(self.handle, lambda session: session.prompted())
                self.store.record_stop(self.handle, self.store_module.Checkout.probe(self.root))
            return native(method, params)
        self.server.request.side_effect = request
        self.assertEqual(0, self.wait())
        self.assertEqual("closed", self.row().end_reason)

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
        LIFECYCLE.run("stop", Host.CODEX, {"session_id": "one", "turn_id": "turn-one"})
        self.assertTrue(WATCHER.canonical_path(self.request).exists())
        self.assertEqual(0, self.wait())
        self.assertEqual("cancelled", self.outcome()["status"])
        self.assertIsNone(self.row().ended_at)


class ClaudeWatcherTests(CloseFixture, unittest.TestCase):
    def setUp(self):
        self.setup_close("claude")

    def test_full_claude_lifecycle_records_end_only_after_exact_exit(self):
        self.arm()
        self.stop()
        self.assertEqual(0, self.wait())
        self.assertEqual("closed", self.outcome()["status"])
        self.assertEqual("closed", self.row().end_reason)

    def test_user_changes_inside_held_handle_preserve_claude(self):
        self.arm()
        self.stop()
        signals = []
        class Primitive:
            def stop(_self, identity, *, before_signal):
                self.append({"type": "user", "uuid": "later", "message": {"content": "continue"}})
                before_signal()
                signals.append(True)
        with mock.patch.object(processes, "stop", side_effect=Primitive().stop):
            self.assertEqual(0, self.wait())
        self.assertEqual([], signals)
        self.assertIsNone(self.row().ended_at)


if __name__ == "__main__":
    unittest.main()
