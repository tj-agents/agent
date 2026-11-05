import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from close_fixtures import LIFECYCLE, CloseFixture, Host, session_close


class CloseLifecycleTests(CloseFixture, unittest.TestCase):
    def setUp(self):
        self.setup_close()

    def prepare(self, intent="task-completion", receipt=True, reason=None):
        return LIFECYCLE.prepare(
            Host(self.host),
            "one",
            self.profile,
            intent,
            session_close,
            self.receipt() if receipt else None,
            dismissal_reason=reason,
        )

    def test_unarmed_stop_is_silent_and_never_assesses_or_spawns(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                0,
                LIFECYCLE.run(
                    "stop", Host(self.host), {"session_id": "one", "turn_id": "turn-one"}
                ),
            )
        self.assertEqual("", output.getvalue())
        self.assertFalse(LIFECYCLE._state_path("one").exists())
        self.spawn.assert_not_called()

    def test_prompt_cancels_an_armed_release_before_recording_the_new_input(self):
        self.arm()
        request = self.request
        self.assertEqual(
            0,
            LIFECYCLE.run(
                "prompt", Host(self.host), {"session_id": "one", "turn_id": "later"}
            ),
        )
        self.assertEqual(
            {"cancelled": True, "reason": "new prompt"},
            json.loads(Path(request["cancel"]).read_text(encoding="utf-8")),
        )
        state = LIFECYCLE._read(LIFECYCLE._state_path("one"))
        self.assertEqual("cancelled", state["status"])
        self.assertEqual("later", LIFECYCLE._read(LIFECYCLE._input_path("one"))["turn_id"])

    def test_matching_stop_only_publishes_completion_for_the_armed_request(self):
        self.arm()
        self.assertEqual(
            0,
            LIFECYCLE.run(
                "stop", Host(self.host), {"session_id": "one", "turn_id": "turn-one"}
            ),
        )
        self.assertEqual(
            {"token": "token"},
            json.loads(Path(self.request["completion"]).read_text(encoding="utf-8")),
        )

    def test_stale_stop_cannot_complete_the_armed_release(self):
        self.arm()
        self.assertEqual(
            0,
            LIFECYCLE.run(
                "stop", Host(self.host), {"session_id": "one", "turn_id": "later"}
            ),
        )
        self.assertFalse(Path(self.request["completion"]).exists())

    def test_keep_open_policy_survives_new_input_until_revoked(self):
        self.assertEqual(
            {"status": "keep-open", "session_id": "one"},
            LIFECYCLE.policy(Host(self.host), "one", self.profile, "keep-open", "user asked to continue"),
        )
        with self.assertRaisesRegex(ValueError, "keep-open"):
            self.prepare()
        self.assertEqual(
            {"status": "revoked", "session_id": "one"},
            LIFECYCLE.policy(Host(self.host), "one", self.profile, "revoke", "user revoked the policy"),
        )
        self.assertEqual("prepared", self.prepare()["status"])

    def test_task_completion_requires_an_accepted_audited_receipt(self):
        receipt = self.receipt("candidate")
        with self.assertRaisesRegex(ValueError, "not accepted"):
            LIFECYCLE.prepare(
                Host(self.host), "one", self.profile, "task-completion", session_close, receipt
            )

    def test_task_completion_refuses_an_unnecessary_audit(self):
        receipt = self.receipt()
        accepted = json.loads(receipt.read_text(encoding="utf-8"))
        accepted["audit"]["state"] = "unnecessary"
        receipt.write_text(json.dumps(accepted), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "audit is incomplete"):
            LIFECYCLE.prepare(
                Host(self.host), "one", self.profile, "task-completion", session_close, receipt
            )

    def test_task_completion_refuses_a_receipt_with_a_changed_generation(self):
        receipt = self.receipt()
        accepted = json.loads(receipt.read_text(encoding="utf-8"))
        accepted["acceptance_generation"] = "changed"
        receipt.write_text(json.dumps(accepted), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "candidate binding changed"):
            LIFECYCLE.prepare(
                Host(self.host), "one", self.profile, "task-completion", session_close, receipt
            )

    def test_task_completion_refuses_receipt_with_deterministic_blockers(self):
        receipt = self.receipt()
        accepted = json.loads(receipt.read_text(encoding="utf-8"))
        accepted["deterministic"] = {"blockers": ["missing cleanup proof"]}
        receipt.write_text(json.dumps(accepted), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "deterministic blockers"):
            LIFECYCLE.prepare(
                Host(self.host), "one", self.profile, "task-completion", session_close, receipt
            )

    def test_completed_inventory_entries_do_not_block_task_completion(self):
        receipt = self.receipt()
        accepted = json.loads(receipt.read_text(encoding="utf-8"))
        accepted["aggregate"] = {
            "known": True,
            "owned": [{"complete": True}],
            "responsibilities": [{"resolved": True}],
            "children": [],
            "waits": [],
        }
        receipt.write_text(json.dumps(accepted), encoding="utf-8")
        self.assertEqual(
            "prepared",
            LIFECYCLE.prepare(
                Host(self.host), "one", self.profile, "task-completion", session_close, receipt
            )["status"],
        )

    def test_recheck_refuses_a_changed_frozen_source(self):
        prepared = self.prepare()
        self.target.write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source changed"):
            LIFECYCLE.recheck(
                Host(self.host), "one", self.profile, prepared["release_generation"], session_close
            )

    def test_recheck_refuses_a_changed_audit_result(self):
        prepared = self.prepare()
        (self.root / "audit-result.json").write_text("{\"findings\": [\"changed\"]}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "audit result changed"):
            LIFECYCLE.recheck(
                Host(self.host), "one", self.profile, prepared["release_generation"], session_close
            )

    def test_retired_goal_uses_the_authorized_checkpoint_without_a_new_audit(self):
        receipt, _checkpoint = self.retired_goal_receipt()
        first = LIFECYCLE.prepare(
            Host(self.host), "one", self.profile, "task-completion", session_close, receipt
        )
        self.assertEqual(
            "rechecked",
            LIFECYCLE.recheck(
                Host(self.host), "one", self.profile, first["release_generation"], session_close
            )["status"],
        )
        second = LIFECYCLE.prepare(
            Host(self.host), "one", self.profile, "task-completion", session_close, receipt
        )
        self.assertEqual("prepared", second["status"])
        self.spawn.assert_not_called()

    def test_retired_goal_refuses_a_changed_checkpoint(self):
        receipt, checkpoint = self.retired_goal_receipt()
        checkpoint.write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checkpoint source changed"):
            LIFECYCLE.prepare(
                Host(self.host), "one", self.profile, "task-completion", session_close, receipt
            )

    def test_pending_cleanup_with_a_live_exact_worker_allows_arm(self):
        prepared = LIFECYCLE.prepare(
            Host(self.host),
            "one",
            self.profile,
            "task-completion",
            session_close,
            self.pending_cleanup_receipt(),
        )
        outcome = LIFECYCLE.arm(
            Host(self.host), "one", self.profile, prepared["release_generation"], session_close
        )
        self.assertEqual("armed", outcome["status"])

    def test_pending_cleanup_without_a_live_exact_worker_refuses_arm(self):
        receipt = self.pending_cleanup_receipt(worker=False)
        prepared = LIFECYCLE.prepare(
            Host(self.host), "one", self.profile, "task-completion", session_close, receipt
        )
        with self.assertRaisesRegex(ValueError, "worker"):
            LIFECYCLE.arm(
                Host(self.host), "one", self.profile, prepared["release_generation"], session_close
            )
        self.assertEqual("accepted", json.loads(receipt.read_text(encoding="utf-8"))["state"])

    def test_pending_cleanup_with_a_mismatched_worker_refuses_arm(self):
        receipt = self.pending_cleanup_receipt(mismatch="owner")
        prepared = LIFECYCLE.prepare(
            Host(self.host), "one", self.profile, "task-completion", session_close, receipt
        )
        with self.assertRaisesRegex(ValueError, "worker receipt changed"):
            LIFECYCLE.arm(
                Host(self.host), "one", self.profile, prepared["release_generation"], session_close
            )

    def test_user_dismissal_keeps_its_explicit_authority_in_the_request(self):
        receipt = self.receipt("candidate")
        checkpoint = json.loads(receipt.read_text(encoding="utf-8"))
        checkpoint["aggregate"] = {"known": False}
        checkpoint.pop("audit")
        receipt.write_text(json.dumps(checkpoint), encoding="utf-8")
        outcome = LIFECYCLE.dismiss(
            Host(self.host),
            "one",
            self.profile,
            receipt,
            "user explicitly dismissed unfinished work",
            session_close,
        )
        request = json.loads(Path(outcome["request"]).read_text(encoding="utf-8"))
        self.assertEqual("user-dismissal", request["intent"])
        self.assertEqual("user explicitly dismissed unfinished work", request["dismissal_reason"])

    def test_refresh_restart_does_not_require_an_acceptance_receipt(self):
        prepared = self.prepare("refresh-restart", receipt=False)
        self.assertIsNone(prepared["acceptance_sha256"])
        outcome = LIFECYCLE.arm(
            Host(self.host), "one", self.profile, prepared["release_generation"], session_close
        )
        request = json.loads(Path(outcome["request"]).read_text(encoding="utf-8"))
        self.assertEqual("refresh-restart", request["intent"])
        self.assertIsNone(request["receipt"])

    def test_cli_loads_its_packaged_peer_runtime_in_an_isolated_process(self):
        script = Path(__file__).resolve().parents[1] / "plugins/agent/.agents/agent/runtime/close_lifecycle.py"
        environment = {**os.environ, "AGENT_STATE_DIRECTORY": str(self.root), "CODEX_HOME": str(self.profile)}
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(script), "policy", "--host", "codex",
             "--session-id", "one", "--profile", str(self.profile), "--action", "keep-open",
             "--reason", "isolated runtime import"],
            capture_output=True,
            encoding="utf-8",
            check=False,
            env=environment,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("keep-open", json.loads(result.stdout)["status"])

    def test_watcher_loads_its_packaged_lifecycle_in_an_isolated_process(self):
        directory = self.root / "terminal-release"
        input_state = {"host": "codex", "session_id": "one", "turn_id": "turn-one",
                       "profile": str(self.profile.resolve()), "transcript_path": None,
                       "input_generation": "input-one"}
        LIFECYCLE._write(directory / "inputs/one.json", input_state)
        request_path = directory / "requests/token.json"
        request = {"version": 1, "status": "armed", "host": "codex", "session_id": "one",
                   "intent": "refresh-restart", "receipt": None, "completion_token": "token",
                   "completion": str(directory / "requests/token.complete.json"),
                   "cancel": str(directory / "requests/token.cancel.json"),
                   "result": str(directory / "requests/token.result.json"),
                   "acknowledgement": str(directory / "requests/token.ack.json"),
                   "entry": {}, "registry": str(self.root / "cli-sessions/one.json"),
                   "native": {**input_state, "input_generation": "replaced"}, "armed_at": 0.0}
        raw = json.dumps(request, sort_keys=True).encode("utf-8") + b"\n"
        request_path.parent.mkdir(parents=True)
        request_path.write_bytes(raw)
        LIFECYCLE._write(directory / "one.json", request)
        script = Path(__file__).resolve().parents[1] / "plugins/agent/.agents/agent/utility/peer/scripts/client_close_watcher.py"
        environment = {**os.environ, "AGENT_STATE_DIRECTORY": str(self.root), "CODEX_HOME": str(self.profile)}
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(script), "--request", str(request_path),
             "--request-sha256", hashlib.sha256(raw).hexdigest()],
            capture_output=True,
            encoding="utf-8",
            check=False,
            env=environment,
            timeout=10,
        )
        self.assertNotIn("ModuleNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()
