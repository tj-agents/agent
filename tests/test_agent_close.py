"""Completion decisions survive unavailable attribution without weakening close proof."""

import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from close_fixtures import LIFECYCLE, CloseFixture, Host


class AgentCloseTests(CloseFixture, unittest.TestCase):
    def setUp(self):
        self.setup_close()

    def assessment(self):
        return json.loads((self.root / "close-lifecycle/assessments/one.json").read_text())

    def test_unfinished_decision_does_not_require_any_frontend_registration(self):
        self.registry.unlink()
        self.assertEqual(0, self.assess("keep-open", "unfinished validation"))
        self.assertEqual("kept-open", self.assessment()["status"])
        self.assertEqual("unfinished validation", self.assessment()["reason"])
        self.spawn.assert_not_called()
        self.primitive_stop.assert_not_called()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            LIFECYCLE.run("stop", Host.CODEX, {"session_id": "one", "turn_id": "turn-one"})
        self.assertEqual("", output.getvalue())

    def test_missing_host_in_old_registration_does_not_block_native_attribution(self):
        record = json.loads(self.registry.read_text())
        record.pop("host")
        self.write(self.registry, record)
        self.arm()
        self.assertEqual("codex", self.request["entry"]["host"])

    def test_unavailable_close_records_the_failure_for_this_input(self):
        self.recording.with_name("client.json").unlink()
        self.assertEqual(2, self.assess("complete"))
        self.assertEqual("unavailable", self.assessment()["status"])
        self.assertIn("Cannot attribute", self.assessment()["reason"])
        self.assertTrue(LIFECYCLE.assessed(Host.CODEX, "one", "turn-one", {}))
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
        self.assertEqual(2, self.assess("complete"))
        self.spawn.assert_not_called()

    def test_new_prompt_during_publication_rolls_back_the_arm(self):
        original = Path.open
        def changed(path, mode="r", *args, **kwargs):
            if path == self.root / "close-lifecycle/one.json" and mode == "xb":
                LIFECYCLE.run("prompt", Host.CODEX, {"session_id": "one", "turn_id": "later"})
            return original(path, mode, *args, **kwargs)
        with mock.patch.object(Path, "open", changed):
            self.assertEqual(2, self.assess("complete"))
        self.assertFalse((self.root / "close-lifecycle/one.json").exists())
        self.assertFalse((self.root / "close-lifecycle/requests/token.json").exists())
        self.spawn.assert_not_called()

    def test_spawn_failure_cleans_publication_and_allows_retry(self):
        self.spawn.side_effect = RuntimeError("spawn failed")
        self.assertEqual(2, self.assess("complete"))
        self.assertFalse((self.root / "close-lifecycle/one.json").exists())
        self.assertFalse((self.root / "close-lifecycle/requests/token.json").exists())
        self.assertEqual("spawn failed", self.assessment()["reason"])
        self.spawn.side_effect = None
        self.assertEqual(0, self.assess("complete"))

    def test_repeated_complete_preserves_the_original_pending_request(self):
        self.arm()
        canonical = self.root / "close-lifecycle/one.json"
        original = canonical.read_bytes()
        self.assertEqual(2, self.assess("complete"))
        self.assertEqual(original, canonical.read_bytes())
        self.spawn.assert_called_once()

    def test_explicit_keep_open_cancels_an_already_pending_close(self):
        self.arm()
        self.assertEqual(0, self.assess("keep-open", "user asked to keep this open"))
        self.assertTrue(Path(self.request["cancel"]).exists())
        self.assertEqual("kept-open", self.assessment()["status"])

    def test_keep_open_during_watcher_start_preserves_the_later_assessment(self):
        def keep_open(*args):
            self.assertEqual(0, self.assess("keep-open", "user asked to keep open"))
            return 99
        self.spawn.side_effect = keep_open
        self.assertEqual(0, self.assess("complete"))
        self.assertEqual("kept-open", self.assessment()["status"])
        self.assertTrue((self.root / "close-lifecycle/requests/token.cancel.json").is_file())

    def test_changed_assessment_cancels_the_watcher_before_signalling(self):
        self.arm()
        state = self.assessment()
        state["status"] = "unavailable"
        LIFECYCLE.record_assessment(state)
        self.assertEqual(0, self.wait())
        self.assertEqual("cancelled", self.outcome()["status"])
        self.primitive_stop.assert_not_called()


class ClaudeAssessmentTests(CloseFixture, unittest.TestCase):
    def setUp(self):
        self.setup_close("claude")

    def assessment(self):
        return json.loads((self.root / "close-lifecycle/assessments/one.json").read_text())

    def test_complete_requires_the_verified_startup_receipt(self):
        record = json.loads(self.registry.read_text())
        Path(record["provenance"]["receipt"]).unlink()
        self.assertEqual(2, self.assess("complete"))
        self.assertEqual("unavailable", self.assessment()["status"])
        self.assertEqual(0, self.assess("keep-open", "receipt unavailable"))
        self.spawn.assert_not_called()

    def test_complete_binds_the_current_transcript_user(self):
        self.arm()
        self.assertEqual("user-one", self.request["claude_user_uuid"])
        self.server.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
