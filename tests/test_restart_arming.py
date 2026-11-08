"""The restart arm/watch pipeline that agent:refresh-plugins relies on to close the old client."""

from pathlib import Path
import unittest
from unittest import mock

from close_fixtures import LIFECYCLE, CloseFixture, Host


class RestartArmingTests(CloseFixture, unittest.TestCase):
    def setUp(self):
        self.setup_close()

    def assessment(self):
        return LIFECYCLE._read(self.root / "close-lifecycle/assessments/one.json")

    def test_missing_host_in_old_registration_does_not_block_native_attribution(self):
        record = LIFECYCLE._read(self.registry)
        record.pop("host")
        self.write(self.registry, record)
        self.arm()
        self.assertEqual("codex", self.request["entry"]["host"])

    def test_unavailable_close_records_the_failure_for_this_input(self):
        self.recording.with_name("client.json").unlink()
        self.assertEqual(2, self.assess())
        self.assertEqual("unavailable", self.assessment()["status"])
        self.assertIn("Cannot attribute", self.assessment()["reason"])
        self.assertFalse((self.root / "close-lifecycle/one.json").exists())
        self.spawn.assert_not_called()
        self.primitive_stop.assert_not_called()

    def test_missing_native_input_receipt_never_fabricates_an_assessment(self):
        LIFECYCLE._input_path("one").unlink()
        self.assertEqual(2, self.assess())
        self.assertFalse((self.root / "close-lifecycle/assessments/one.json").exists())
        self.spawn.assert_not_called()

    def test_new_native_input_during_attribution_cannot_arm(self):
        self.native_input = "other-input"
        self.assertEqual(2, self.assess())
        self.spawn.assert_not_called()

    def test_new_prompt_during_publication_rolls_back_the_arm(self):
        original = Path.open
        def changed(path, mode="r", *args, **kwargs):
            if path == self.root / "close-lifecycle/one.json" and mode == "xb":
                LIFECYCLE.run("prompt", Host.CODEX, {"session_id": "one", "turn_id": "later"})
            return original(path, mode, *args, **kwargs)
        with mock.patch.object(Path, "open", changed):
            self.assertEqual(2, self.assess())
        self.assertFalse((self.root / "close-lifecycle/one.json").exists())
        self.assertFalse((self.root / "close-lifecycle/requests/token.json").exists())
        self.spawn.assert_not_called()

    def test_spawn_failure_cleans_publication_and_allows_retry(self):
        self.spawn.side_effect = RuntimeError("spawn failed")
        self.assertEqual(2, self.assess())
        self.assertFalse((self.root / "close-lifecycle/one.json").exists())
        self.assertFalse((self.root / "close-lifecycle/requests/token.json").exists())
        self.assertEqual("spawn failed", self.assessment()["reason"])
        self.spawn.side_effect = None
        self.assertEqual(0, self.assess())

    def test_repeated_restart_preserves_the_original_pending_request(self):
        self.arm()
        canonical = self.root / "close-lifecycle/one.json"
        original = canonical.read_bytes()
        self.assertEqual(2, self.assess())
        self.assertEqual(original, canonical.read_bytes())
        self.spawn.assert_called_once()

    def test_changed_assessment_cancels_the_watcher_before_signalling(self):
        self.arm()
        state = self.assessment()
        state["status"] = "unavailable"
        LIFECYCLE.record_assessment(state)
        self.assertEqual(0, self.wait())
        self.assertEqual("cancelled", self.outcome()["status"])
        self.primitive_stop.assert_not_called()

    def test_a_new_prompt_cancels_an_armed_restart_before_it_settles(self):
        self.arm()
        LIFECYCLE.run("prompt", Host.CODEX, {"session_id": "one", "turn_id": "turn-two"})
        self.assertEqual(0, self.wait())
        self.assertEqual("cancelled", self.outcome()["status"])
        self.primitive_stop.assert_not_called()

    def test_the_old_client_is_stopped_once_the_armed_turn_settles(self):
        self.arm()
        self.stop()
        self.assertEqual(0, self.wait())
        self.assertEqual("closed", self.outcome()["status"])
        self.primitive_stop.assert_called_once()


class ClaudeRestartArmingTests(CloseFixture, unittest.TestCase):
    def setUp(self):
        self.setup_close("claude")

    def assessment(self):
        return LIFECYCLE._read(self.root / "close-lifecycle/assessments/one.json")

    def test_restart_requires_the_verified_startup_receipt(self):
        record = LIFECYCLE._read(self.registry)
        Path(record["provenance"]["receipt"]).unlink()
        self.assertEqual(2, self.assess())
        self.assertEqual("unavailable", self.assessment()["status"])
        self.spawn.assert_not_called()

    def test_restart_binds_the_current_transcript_user(self):
        self.arm()
        self.assertEqual("user-one", self.request["claude_user_uuid"])
        self.server.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
