"""Preparation and arming use fresh native proof and leave no failed arm."""

import json
from pathlib import Path
import unittest
from unittest import mock

from close_fixtures import CLOSE, CloseFixture, session_close


class AgentCloseTests(CloseFixture, unittest.TestCase):
    def setUp(self):
        self.setup_close()

    def test_preparation_records_current_input_without_starting_a_watcher(self):
        self.assertEqual(0, self.prepare())
        assessment = self.root / "close-lifecycle/assessments/one.json"
        self.assertEqual({"host": "codex", "session_id": "one", "turn_id": "turn-one"},
                         json.loads(assessment.read_text(encoding="utf-8")))
        self.assertFalse((self.root / "close-lifecycle/one.json").exists())
        self.spawn.assert_not_called()

    def test_new_codex_registration_is_attributed_before_active_turn_arm(self):
        before = self.row()
        self.assertNotIn("client_identity", json.loads(self.registry.read_text(encoding="utf-8")))
        self.arm()
        self.assertEqual("managed-input", self.request["entry"]["client_identity"])
        self.assertEqual(20, self.row()["pid"])
        self.assertEqual(before["last_activity_at"], self.row()["last_activity_at"])
        self.assertEqual("working", self.row()["turn_state"])
        self.assertIsNone(self.request["working_activity"])
        self.assertIn("native_completion", self.request)
        self.assertEqual("close", self.request["mode"])
        self.assertEqual(2, CLOSE.main(self.arm_args()))
        self.spawn.assert_called_once()

    def test_missing_or_unsupported_cli_leaves_no_arm_and_corrected_retry_succeeds(self):
        self.assertEqual(0, self.prepare())
        original = self.capability
        self.capability = self.root / "missing.py"
        self.assertEqual(2, CLOSE.main(self.arm_args()))
        self.assertFalse((self.root / "close-lifecycle/one.json").exists())
        self.capability = original
        primitive = original.with_name("process_identity.py")
        source = primitive.read_text(encoding="utf-8")
        primitive.write_text("# unsupported capability\n", encoding="utf-8")
        self.assertEqual(2, CLOSE.main(self.arm_args()))
        self.assertFalse((self.root / "close-lifecycle/one.json").exists())
        primitive.write_text(source, encoding="utf-8")
        self.assertEqual(0, CLOSE.main(self.arm_args()))

    def test_spawn_failure_cleans_publication_and_preserves_original_explicit_arm(self):
        self.assertEqual(0, self.prepare())
        self.spawn.side_effect = RuntimeError("spawn failed")
        self.assertEqual(2, CLOSE.main(self.arm_args()))
        self.assertFalse((self.root / "close-lifecycle/one.json").exists())
        self.assertFalse((self.root / "client-close/token.json").exists())
        self.spawn.side_effect = None
        self.assertEqual(0, CLOSE.main(self.arm_args()))
        canonical = self.root / "close-lifecycle/one.json"
        original = canonical.read_bytes()
        self.assertEqual(2, CLOSE.main(self.arm_args("new-token")))
        self.assertEqual(original, canonical.read_bytes())

    def test_input_change_between_prepare_and_arm_never_retargets(self):
        self.assertEqual(0, self.prepare())
        self.native_input = "other-input"
        self.assertEqual(2, CLOSE.main(self.arm_args()))
        self.spawn.assert_not_called()

    def test_failed_canonical_publication_removes_request_and_allows_retry(self):
        self.assertEqual(0, self.prepare())
        canonical = self.root / "close-lifecycle/one.json"
        original_open = Path.open
        def fail_state(path, mode="r", *args, **kwargs):
            if path == canonical and mode == "xb":
                raise OSError("publication failed")
            return original_open(path, mode, *args, **kwargs)
        with mock.patch.object(Path, "open", fail_state):
            self.assertEqual(2, CLOSE.main(self.arm_args()))
        self.assertFalse(canonical.exists())
        self.assertFalse((self.root / "client-close/token.json").exists())
        self.assertEqual(0, CLOSE.main(self.arm_args()))

    def test_token_paths_and_nested_capability_boundary_refuse(self):
        self.assertEqual(0, self.prepare())
        for token in ("../escape", "..", "a/b", "a\\b", "bad:stream"):
            with self.subTest(token=token):
                self.assertEqual(2, CLOSE.main(self.arm_args(token)))
        nested = self.cli / "nested"
        (nested / ".git").mkdir(parents=True)
        self.capability = nested / "close.py"
        self.capability.touch()
        self.assertEqual(2, CLOSE.main(self.arm_args()))
        self.spawn.assert_not_called()


class ClaudePreparationTests(CloseFixture, unittest.TestCase):
    def setUp(self):
        self.setup_close("claude")

    def test_verified_startup_receipt_and_bound_user_prepare_and_arm(self):
        self.arm()
        self.assertEqual("user-one", self.request["claude_user_uuid"])
        self.assertNotIn("client_identity", self.request["entry"])
        self.server.request.assert_not_called()

    def test_changed_receipt_or_new_user_refuses(self):
        self.assertEqual(0, self.prepare())
        self.append({"type": "user", "uuid": "later", "message": {"content": "continue"}})
        self.assertEqual(2, CLOSE.main(self.arm_args()))
        entry = json.loads(self.registry.read_text(encoding="utf-8"))
        Path(entry["provenance"]["receipt"]).unlink()
        self.assertEqual(2, self.prepare())
        self.spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
