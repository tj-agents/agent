import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "plugins" / "agent" / ".agents" / "agent" / "runtime" / "close_lifecycle.py"
spec = importlib.util.spec_from_file_location("close_lifecycle", PATH)
LIFECYCLE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(LIFECYCLE)
from host import Host  # noqa: E402
from host_sessions import SESSIONS  # noqa: E402


class CloseLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.previous = os.environ.get("AGENT_STATE_DIRECTORY")
        os.environ["AGENT_STATE_DIRECTORY"] = str(self.state)
        self.addCleanup(self.restore_environment)

    def restore_environment(self):
        if self.previous is None:
            os.environ.pop("AGENT_STATE_DIRECTORY", None)
        else:
            os.environ["AGENT_STATE_DIRECTORY"] = self.previous

    def arm(self, turn="first"):
        completion = self.state / "completion.json"
        cancel = self.state / "cancel.json"
        LIFECYCLE._write(LIFECYCLE._state_path("session"), {
            "host": "codex", "session_id": "session", "turn_id": turn,
            "completion": str(completion), "cancel": str(cancel), "completion_token": "token",
        })
        return completion, cancel

    def arm_claude(self, transcript, token):
        completion = self.state / "claude-completion.json"
        cancel = self.state / "claude-cancel.json"
        LIFECYCLE._write(LIFECYCLE._state_path("session"), {
            "host": "claude", "session_id": "session", "turn_id": None,
            "completion": str(completion), "cancel": str(cancel), "completion_token": "token",
            "transcript": str(transcript), "claude_user_uuid": token,
        })
        return completion, cancel

    def append(self, path, record):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")

    def stop_output(self, data, host=Host.CODEX):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, LIFECYCLE.run("stop", host, data))
        return json.loads(output.getvalue()) if output.getvalue() else None

    def test_omitted_close_selection_requests_assessment_without_arming(self):
        result = self.stop_output({"session_id": "session", "turn_id": "merged-template53"})
        self.assertEqual("block", result["decision"])
        self.assertIn("agent:close", result["reason"])
        self.assertIn("unfinished", result["reason"])
        self.assertFalse(LIFECYCLE._state_path("session").exists())
        self.assertEqual([], list(self.state.rglob("*")))

    def test_current_assessment_permits_keep_open_without_a_close_request(self):
        LIFECYCLE.record_assessment({"host": "codex", "session_id": "session", "turn_id": "question"})
        self.assertIsNone(self.stop_output({"session_id": "session", "turn_id": "question"}))
        self.assertFalse(LIFECYCLE._state_path("session").exists())

    def test_prior_input_assessment_does_not_cover_a_later_input(self):
        LIFECYCLE.record_assessment({"host": "codex", "session_id": "session", "turn_id": "earlier"})
        self.assertEqual("block", self.stop_output({"session_id": "session", "turn_id": "later"})["decision"])

    def test_reentrant_stop_does_not_loop_when_assessment_is_unavailable(self):
        self.assertIsNone(self.stop_output({"session_id": "session", "turn_id": "turn", "stop_hook_active": True}))
        self.assertEqual([], list(self.state.rglob("*")))

    def test_stop_without_native_input_does_not_infer_completion(self):
        self.assertIsNone(self.stop_output({"session_id": "session", "cwd": "merged-worktree"}))

    def test_disarmed_state_does_not_hide_an_omitted_assessment(self):
        LIFECYCLE._write(LIFECYCLE._state_path("session"), {"host": "codex", "session_id": "session"})
        self.assertEqual("block", self.stop_output({"session_id": "session", "turn_id": "turn"})["decision"])

    def test_claude_assessment_covers_only_the_exact_transcript_user(self):
        transcript = self.state / "session.jsonl"
        self.append(transcript, {"type": "user", "uuid": "one", "message": {"content": "question"}})
        self.append(transcript, {"type": "assistant", "message": {"stop_reason": "end_turn"}})
        data = {"session_id": "session", "transcript_path": str(transcript)}
        self.assertEqual("block", self.stop_output(data, Host.CLAUDE)["decision"])
        LIFECYCLE.record_assessment({"host": "claude", "session_id": "session", "turn_id": None,
                                     "transcript": str(transcript), "claude_user_uuid": "one"})
        self.assertIsNone(self.stop_output(data, Host.CLAUDE))
        self.assertFalse(LIFECYCLE._state_path("session").exists())
        self.append(transcript, {"type": "user", "uuid": "two", "message": {"content": "continue"}})
        self.append(transcript, {"type": "assistant", "message": {"stop_reason": "end_turn"}})
        self.assertEqual("block", self.stop_output(data, Host.CLAUDE)["decision"])

    def test_new_input_cancels_matching_armed_turn(self):
        _completion, cancel = self.arm()
        self.assertEqual(0, LIFECYCLE.run("prompt", Host.CODEX, {"session_id": "session", "turn_id": "next-turn"}))
        self.assertEqual({"cancelled": True, "reason": "new prompt"}, json.loads(cancel.read_text(encoding="utf-8")))
        self.assertTrue(LIFECYCLE._state_path("session").exists())

    def test_canceled_request_does_not_cover_a_new_input_assessment(self):
        completion, cancel = self.arm("old")
        self.assertEqual(0, LIFECYCLE.run("prompt", Host.CODEX, {"session_id": "session", "turn_id": "new"}))
        self.assertTrue(cancel.exists())
        self.assertEqual("block", self.stop_output({"session_id": "session", "turn_id": "new"})["decision"])
        self.assertFalse(completion.exists())
        self.assertEqual("old", json.loads(LIFECYCLE._state_path("session").read_text())["turn_id"])

    def test_stale_stop_cannot_close_a_later_turn(self):
        completion, _cancel = self.arm("later")
        self.assertEqual(0, LIFECYCLE.run("stop", Host.CODEX, {"session_id": "session", "turn_id": "earlier"}))
        self.assertFalse(completion.exists())
        self.assertTrue(LIFECYCLE._state_path("session").exists())
        self.assertEqual(0, LIFECYCLE.run("stop", Host.CODEX, {"session_id": "session", "turn_id": "later"}))
        self.assertEqual({"token": "token"}, json.loads(completion.read_text(encoding="utf-8")))
        self.assertTrue(LIFECYCLE._state_path("session").exists())
        self.assertEqual(0, LIFECYCLE.run("prompt", Host.CODEX, {"session_id": "session", "turn_id": "next"}))
        self.assertTrue(LIFECYCLE._state_path("session").exists())

    def test_claude_stop_requires_terminal_assistant_after_bound_user(self):
        transcript = self.state / "session.jsonl"
        self.append(transcript, {"type": "user", "uuid": "one", "message": {"content": "close"}})
        completion, _cancel = self.arm_claude(transcript, "one")
        self.append(transcript, {"type": "assistant", "message": {"stop_reason": "tool_use"}})
        self.assertEqual(0, LIFECYCLE.run("stop", Host.CLAUDE, {"session_id": "session", "transcript_path": str(transcript)}))
        self.assertFalse(completion.exists())
        self.append(transcript, {"type": "assistant", "message": {"stop_reason": "end_turn"}})
        self.assertEqual(0, LIFECYCLE.run("stop", Host.CLAUDE, {"session_id": "session", "transcript_path": str(transcript)}))
        self.assertEqual({"token": "token"}, json.loads(completion.read_text(encoding="utf-8")))

    def test_claude_late_stop_cannot_complete_second_user_before_final_answer(self):
        transcript = self.state / "session.jsonl"
        self.append(transcript, {"type": "user", "uuid": "one", "message": {"content": "first"}})
        _completion, cancel = self.arm_claude(transcript, "one")
        self.assertEqual(0, LIFECYCLE.run("prompt", Host.CLAUDE, {"session_id": "session"}))
        self.assertTrue(cancel.exists())
        self.append(transcript, {"type": "user", "uuid": "two", "message": {"content": "second"}})
        completion, _cancel = self.arm_claude(transcript, "two")
        self.assertEqual(0, LIFECYCLE.run("stop", Host.CLAUDE, {"session_id": "session", "transcript_path": str(transcript)}))
        self.assertFalse(completion.exists())

    def test_claude_streams_past_large_tool_result_to_bind_and_finish_user_turn(self):
        transcript = self.state / "session.jsonl"
        self.append(transcript, {"type": "user", "uuid": "one", "message": {"content": "close"}})
        self.append(transcript, {"type": "user", "uuid": "tool", "message": {"content": [
            {"type": "tool_result", "content": "x" * (256 * 1024 + 1)}]}})
        self.append(transcript, {"type": "assistant", "message": {"stop_reason": "end_turn"}})
        completion, _cancel = self.arm_claude(transcript, "one")
        self.assertEqual("one", SESSIONS[Host.CLAUDE].user_turn(transcript))
        self.assertEqual(0, LIFECYCLE.run("stop", Host.CLAUDE, {"session_id": "session", "transcript_path": str(transcript)}))
        self.assertEqual({"token": "token"}, json.loads(completion.read_text(encoding="utf-8")))

    def test_claude_later_user_replaces_bound_turn(self):
        transcript = self.state / "session.jsonl"
        self.append(transcript, {"type": "user", "uuid": "one", "message": {"content": "first"}})
        completion, _cancel = self.arm_claude(transcript, "one")
        self.append(transcript, {"type": "assistant", "message": {"stop_reason": "end_turn"}})
        self.append(transcript, {"type": "user", "uuid": "two", "message": {"content": "second"}})
        self.append(transcript, {"type": "assistant", "message": {"stop_reason": "end_turn"}})
        self.assertEqual(0, LIFECYCLE.run("stop", Host.CLAUDE, {"session_id": "session", "transcript_path": str(transcript)}))
        self.assertFalse(completion.exists())

    def test_claude_latest_user_without_uuid_does_not_reuse_old_turn(self):
        transcript = self.state / "session.jsonl"
        self.append(transcript, {"type": "user", "uuid": "one", "message": {"content": "first"}})
        completion, _cancel = self.arm_claude(transcript, "one")
        self.append(transcript, {"type": "user", "message": {"content": "second"}})
        self.append(transcript, {"type": "assistant", "message": {"stop_reason": "end_turn"}})
        with self.assertRaisesRegex(ValueError, "no bounded"):
            SESSIONS[Host.CLAUDE].user_turn(transcript)
        self.assertEqual(0, LIFECYCLE.run("stop", Host.CLAUDE, {"session_id": "session", "transcript_path": str(transcript)}))
        self.assertFalse(completion.exists())

    def test_claude_ignores_metadata_and_sidechain_turns(self):
        transcript = self.state / "session.jsonl"
        self.append(transcript, {"type": "user", "uuid": "one", "message": {"content": "close"}})
        completion, _cancel = self.arm_claude(transcript, "one")
        self.append(transcript, {"type": "user", "uuid": "meta", "isMeta": True, "message": {"content": "meta"}})
        self.append(transcript, {"type": "user", "uuid": "compact", "isCompactSummary": True, "message": {"content": "summary"}})
        self.append(transcript, {"type": "user", "uuid": "tool", "message": {"content": [{"type": "tool_result"}]}})
        self.append(transcript, {"type": "user", "uuid": "sidechain", "isSidechain": True, "message": {"content": "sidechain"}})
        self.append(transcript, {"type": "assistant", "message": {"stop_reason": "end_turn"}})
        self.append(transcript, {"type": "assistant", "isSidechain": True, "message": {"stop_reason": "tool_use"}})
        self.assertEqual(0, LIFECYCLE.run("stop", Host.CLAUDE, {"session_id": "session", "transcript_path": str(transcript)}))
        self.assertEqual({"token": "token"}, json.loads(completion.read_text(encoding="utf-8")))

    def test_claude_non_terminal_assistant_after_end_turn_refuses_completion(self):
        transcript = self.state / "session.jsonl"
        self.append(transcript, {"type": "user", "uuid": "one", "message": {"content": "close"}})
        completion, _cancel = self.arm_claude(transcript, "one")
        self.append(transcript, {"type": "assistant", "message": {"stop_reason": "end_turn"}})
        self.append(transcript, {"type": "assistant", "message": {"stop_reason": "tool_use"}})
        self.assertEqual(0, LIFECYCLE.run("stop", Host.CLAUDE, {"session_id": "session", "transcript_path": str(transcript)}))
        self.assertFalse(completion.exists())


if __name__ == "__main__":
    unittest.main()
