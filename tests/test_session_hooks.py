import json
from pathlib import Path
import re
import unittest

PACKAGE = Path(__file__).resolve().parents[1] / "plugins" / "agent"
CLAUDE_ROOT = "${CLAUDE_PLUGIN_ROOT}/"
CODEX_ROOT = "${PLUGIN_ROOT}/"
EVENTS = {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}


def hooks(host):
    return json.loads((PACKAGE / "hooks" / f"{host}.json").read_text(encoding="utf-8"))["hooks"]


def packaged_paths(text, root):
    return re.findall(re.escape(root) + r"([^\"\s]+\.py)", text)


class SessionHookTests(unittest.TestCase):
    def test_both_hosts_register_the_session_lifecycle_events(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                self.assertTrue(EVENTS <= set(hooks(host)))

    def test_lifecycle_decisions_are_synchronous_and_session_end_cancels_first(self):
        for host in ("claude", "codex"):
            for event in ("UserPromptSubmit", "Stop", "SessionEnd"):
                for group in hooks(host)[event]:
                    for hook in group["hooks"]:
                        with self.subTest(host=host, event=event):
                            self.assertFalse(hook.get("async", False))
                            command = json.dumps(hook)
                            self.assertIn("close_lifecycle.py", command)
                            if event == "SessionEnd":
                                self.assertLess(command.index("close_lifecycle.py"),
                                                command.index("session_hook.py"))
                                self.assertLessEqual(hook["timeout"], 3)

    def test_every_script_a_hook_names_ships_in_the_package(self):
        for host, root in (("claude", CLAUDE_ROOT), ("codex", CODEX_ROOT)):
            scripts = packaged_paths(json.dumps(hooks(host)), root)
            self.assertTrue(scripts)
            for script in scripts:
                with self.subTest(host=host, script=script):
                    self.assertTrue((PACKAGE / script).is_file())

    def test_codex_hooks_run_through_the_verified_snapshot_loader(self):
        self.assertTrue((PACKAGE / "hooks" / "codex" / "snapshot.py").is_file())
        for groups in hooks("codex").values():
            for group in groups:
                for hook in group["hooks"]:
                    with self.subTest(command=hook["command"][:60]):
                        self.assertTrue(hook["command"].startswith("tj_hook -- python3 -I -B -c "))
                        self.assertTrue(hook["commandWindows"].startswith("tj_hook -- python -I -B -c "))

    def test_claude_hooks_use_the_native_launcher(self):
        for groups in hooks("claude").values():
            for group in groups:
                for hook in group["hooks"]:
                    with self.subTest(hook=hook):
                        self.assertEqual("tj_hook", hook["command"])
                        self.assertEqual(["--", "python", "-B"], hook["args"][:3])


if __name__ == "__main__":
    unittest.main()
