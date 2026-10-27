import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DISPATCH = ROOT / "plugins" / "agent" / "hooks" / "hook_dispatch.py"
GATES = {
    "crash": "raise RuntimeError('gate crashed')\n",
    "deny": "import sys\nsys.stderr.write('gate refused')\nsys.exit(2)\n",
    "fail": "import sys\nsys.exit(1)\n",
    "noise": "print('not json')\n",
    "slow": "import time\ntime.sleep(30)\n",
}


class HookDispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gates = Path(temporary.name)
        for name, body in GATES.items():
            (self.gates / f"{name}.py").write_text(body, encoding="utf-8")

    def dispatch(self, event, *gates, deadline="5"):
        arguments = [argument for gate in gates for argument in ("--hook", str(self.gates / f"{gate}.py"))]
        return subprocess.run([sys.executable, "-B", str(DISPATCH), "--deadline", deadline, *arguments],
                              input=json.dumps({"hook_event_name": event}), capture_output=True, text=True,
                              timeout=20)

    def test_failing_session_gates_emit_nothing_and_exit_zero(self):
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
            with self.subTest(event=event):
                result = self.dispatch(event, "crash", "deny", "fail")
                self.assertEqual((0, ""), (result.returncode, result.stdout))
                self.assertIn("gate crashed", result.stderr)
                self.assertIn("gate refused", result.stderr)

    def test_overrunning_session_gate_emits_nothing(self):
        for event in ("UserPromptSubmit", "Stop"):
            with self.subTest(event=event):
                result = self.dispatch(event, "slow", deadline="0.5")
                self.assertEqual((0, ""), (result.returncode, result.stdout))
                self.assertIn("slow.py did not finish", result.stderr)

    def test_unstructured_output_outside_session_start_is_only_reported(self):
        result = self.dispatch("Stop", "noise")
        self.assertEqual((0, ""), (result.returncode, result.stdout))
        self.assertIn("not json", result.stderr)

    def test_pre_tool_use_still_denies_on_failure(self):
        result = self.dispatch("PreToolUse", "deny")
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(("deny", "gate refused"), (output["permissionDecision"], output["permissionDecisionReason"]))


if __name__ == "__main__":
    unittest.main()
