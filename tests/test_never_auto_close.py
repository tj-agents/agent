import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "plugins" / "agent"
SESSION = "22176c14-5e45-4d73-a2fb-c906e0147dea"
CLOSE_STATE = ("close-lifecycle", "client-close", "session-close")
LIFECYCLE_SCRIPTS = {"close_lifecycle.py", "hook_dispatch.py", "loader.py", "reap_orphans.py",
                     "register_session.py", "session_store.py", "snapshot.py"}


def manifest(host):
    return json.loads((PACKAGE / "hooks" / f"{host}.json").read_text(encoding="utf-8"))["hooks"]


def claude_hooks(event):
    return [hook for group in manifest("claude").get(event, []) for hook in group["hooks"]]


def command(hook):
    executable = sys.executable if hook["command"] == "python" else hook["command"]
    return [executable, *(argument.replace("${CLAUDE_PLUGIN_ROOT}", str(PACKAGE)) for argument in hook["args"])]


def git(cwd, *arguments):
    subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                    "-C", str(cwd), *arguments], check=True, capture_output=True, text=True)


def record(kind, uuid, content, **extra):
    message = {"role": kind, "content": content, **extra}
    return {"type": kind, "uuid": uuid, "sessionId": SESSION, "isSidechain": False, "message": message}


class NeverAutoCloseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.state = base / "state"
        self.claude = base / "claude"
        repository = base / "python"
        repository.mkdir()
        git(repository, "init", "-q", "-b", "main")
        (repository / "README.md").write_text("fixture\n", encoding="utf-8")
        git(repository, "add", "README.md")
        git(repository, "commit", "-q", "-m", "Initial")
        self.worktree = base / "python.worktrees" / "Feature-EnumVariantDispatch"
        git(repository, "worktree", "add", "-q", "-b", "Feature/EnumVariantDispatch", str(self.worktree))
        self.transcript = self.claude / "projects" / "fixture" / f"{SESSION}.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.environment = {**os.environ, "AGENT_STATE_DIRECTORY": str(self.state),
                            "CLAUDE_CONFIG_DIR": str(self.claude), "CLAUDE_PLUGIN_ROOT": str(PACKAGE)}

    def append(self, *records):
        with self.transcript.open("a", encoding="utf-8") as stream:
            for value in records:
                stream.write(json.dumps(value) + "\n")

    def dispatch(self, event, **payload):
        hooks = claude_hooks(event)
        self.assertTrue(hooks)
        for hook in hooks:
            completed = subprocess.run(command(hook), input=json.dumps({
                "session_id": SESSION, "transcript_path": str(self.transcript), "cwd": str(self.worktree),
                "hook_event_name": event, **payload,
            }), capture_output=True, text=True, cwd=self.worktree, env=self.environment, timeout=hook["timeout"])
            self.assertEqual(0, completed.returncode, completed.stderr)

    def close_requests(self):
        return sorted(str(path.relative_to(self.state)) for name in CLOSE_STATE
                      for path in (self.state / name).rglob("*") if path.is_file())

    def test_ending_a_turn_after_a_rejected_question_in_a_landed_worktree_arms_no_close(self):
        self.append(
            record("user", "u1", "finish the refactor and keep the PRs moving"),
            record("assistant", "a1", [{"type": "tool_use", "id": "toolu_ask", "name": "AskUserQuestion",
                                        "input": {"questions": []}}], stop_reason="tool_use"),
            record("user", "r1", [{"type": "tool_result", "tool_use_id": "toolu_ask", "is_error": True,
                                   "content": "The user doesn't want to proceed with this tool use."}]),
            record("user", "u2", [{"type": "text", "text": "[Request interrupted by user for tool use]"}]),
        )
        self.dispatch("UserPromptSubmit", prompt="what are you doing")
        self.append(record("user", "u3", "what are you doing"),
                    record("assistant", "a2", [{"type": "text", "text": "Stopping. Here is what happened."}],
                           stop_reason="end_turn"))
        self.dispatch("Stop", stop_hook_active=False)
        self.dispatch("Stop", stop_hook_active=False)
        self.assertEqual([], self.close_requests())

    def test_shipped_hooks_run_only_the_session_lifecycle_scripts(self):
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                scripts = {path.rsplit("/", 1)[-1] for path in re.findall(r"[A-Za-z0-9_./-]+\.py", json.dumps(manifest(host)))}
                self.assertTrue(scripts)
                self.assertLessEqual(scripts, LIFECYCLE_SCRIPTS)


if __name__ == "__main__":
    unittest.main()
