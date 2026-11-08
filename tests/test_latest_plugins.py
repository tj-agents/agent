import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "plugins/agent/.agents/agent/runtime/latest_plugins.py"
PIN = "b354217d980a143516a3bd6029d60ffcc9ea3e7b"
OLD = "9e207f3dbe4ce5d06e1a792f6f8edcc366fc4cfd"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class LatestPluginsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.claude = self.home / "claude"
        self.codex = self.home / "codex"
        write(self.claude / "plugins/marketplaces/tj-agents/.claude-plugin/marketplace.json", {"plugins": [
            {"name": "agent", "source": {"source": "git-subdir", "sha": PIN}},
            {"name": "cli", "source": {"source": "git-subdir", "ref": "main"}},
        ]})
        self.install = self.claude / "plugins/cache/tj-agents/agent/current"
        write(self.install / ".codex-plugin/plugin.json", {"name": "agent", "version": "aaaaaaaaaaaa"})

    def registry(self, revision):
        write(self.claude / "plugins/installed_plugins.json", {"version": 2, "plugins": {
            "agent@tj-agents": [{"scope": "user", "gitCommitSha": revision, "installPath": str(self.install)}],
            "cli@tj-agents": [{"scope": "user", "gitCommitSha": OLD, "installPath": str(self.install)}],
        }})

    def codex_build(self, label):
        (self.codex / "plugins/cache/tj-agents/agent" / label).mkdir(parents=True)

    def run_hook(self, host, event, root=None):
        root = root or self.install
        environment = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.codex),
                       "CLAUDE_PLUGIN_ROOT": str(root), "PLUGIN_ROOT": str(root)}
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--host", host],
                                input=json.dumps({"hook_event_name": event}), capture_output=True, text=True,
                                env=environment, timeout=30, check=True)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def test_a_current_session_runs_unchanged(self):
        self.registry(PIN)
        self.codex_build("aaaaaaaaaaaa")
        self.assertIsNone(self.run_hook("claude", "UserPromptSubmit"))
        self.assertIsNone(self.run_hook("codex", "UserPromptSubmit"))

    def test_a_stale_claude_install_refuses_the_prompt_with_the_refresh_step(self):
        self.registry(OLD)
        output = self.run_hook("claude", "UserPromptSubmit")
        self.assertEqual("block", output["decision"])
        self.assertIn("agent@tj-agents: installed 9e207f3dbe4c, marketplace b354217d980a", output["reason"])
        self.assertIn("refresh.py", output["reason"])
        self.assertNotIn("cli@tj-agents", output["reason"])

    def test_a_stale_codex_build_is_refused_and_session_start_says_so(self):
        self.registry(PIN)
        self.codex_build("bbbbbbbbbbbb")
        self.assertEqual("block", self.run_hook("codex", "UserPromptSubmit")["decision"])
        context = self.run_hook("codex", "SessionStart")["hookSpecificOutput"]
        self.assertEqual("SessionStart", context["hookEventName"])
        self.assertIn("installed build bbbbbbbbbbbb, marketplace build aaaaaaaaaaaa", context["additionalContext"])
        self.assertIsNone(self.run_hook("claude", "UserPromptSubmit"))


    def test_a_current_install_still_refuses_a_session_running_an_older_claude_build(self):
        self.registry(PIN)
        running = self.claude / "plugins/cache/tj-agents/agent" / f"{OLD[:12]}-d086935e"
        running.mkdir(parents=True)
        output = self.run_hook("claude", "UserPromptSubmit", running)
        self.assertEqual("block", output["decision"])
        self.assertIn(f"this session loaded {OLD[:12]}, current {PIN[:12]}", output["reason"])
        self.assertIn("/reload-plugins", output["reason"])
        current = self.claude / "plugins/cache/tj-agents/agent" / f"{PIN[:12]}-d086935e"
        current.mkdir(parents=True)
        self.assertIsNone(self.run_hook("claude", "UserPromptSubmit", current))

    def test_a_current_install_still_refuses_a_session_running_an_older_or_removed_codex_build(self):
        self.registry(PIN)
        self.codex_build("aaaaaaaaaaaa")
        older = self.home / "running-codex"
        write(older / ".codex-plugin/plugin.json", {"name": "agent", "version": "cccccccccccc"})
        output = self.run_hook("codex", "UserPromptSubmit", older)
        self.assertEqual("block", output["decision"])
        self.assertIn("loaded build cccccccccccc, current aaaaaaaaaaaa", output["reason"])
        self.assertIn("restart this Codex session", output["reason"])
        removed = self.run_hook("codex", "UserPromptSubmit", self.home / "deleted-build")
        self.assertIn("that was since replaced", removed["reason"])


if __name__ == "__main__":
    unittest.main()
