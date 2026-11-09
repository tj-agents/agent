import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "plugins/agent/.agents/agent/runtime/latest_plugins.py"
PINS = json.loads(SCRIPT.with_name("work-items.json").read_text(encoding="utf-8"))
PIN = "b354217d980a143516a3bd6029d60ffcc9ea3e7b"
OLD = "9e207f3dbe4ce5d06e1a792f6f8edcc366fc4cfd"
UPDATING_CLAUDE = "Updating tj-agents plugins… type /reload-plugins when this says done."
UPDATING_CODEX = "Updating tj-agents plugins… restart this session when this says done."


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_receipt(tools: Path, pins: list[dict[str, str]]) -> None:
    rows = [
        f'{{ name = "{pin["package"]}", '
        f'git = "https://github.com/{pin["repository"]}'
        f"?subdirectory={pin['subdirectory'].replace('/', '%2F')}"
        f'&rev={pin["revision"]}" }},'
        for pin in pins
    ]
    path = tools / "tj-work-items" / "uv-receipt.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(["[tool]", "requirements = [", *rows, "]", ""])
    path.write_text(text, encoding="utf-8")


def isolated_machine(home: Path) -> dict[str, str]:
    return {
        "UV_TOOL_DIR": str(home / "uv-tools"),
        "APPDATA": str(home / "appdata"),
        "LOCALAPPDATA": str(home / "localappdata"),
    }


class LatestPluginsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.claude = self.home / "claude"
        self.codex = self.home / "codex"
        self.state = self.home / "state"
        write(self.claude / "plugins/marketplaces/tj-agents/.claude-plugin/marketplace.json", {"plugins": [
            {"name": "agent", "source": {"source": "git-subdir", "sha": PIN}},
            {"name": "cli", "source": {"source": "git-subdir", "ref": "main"}},
        ]})
        self.install = self.claude / "plugins/cache/tj-agents/agent/current"
        write(self.install / ".codex-plugin/plugin.json", {"name": "agent", "version": "aaaaaaaaaaaa"})
        write_receipt(self.home / "uv-tools", [PINS["tool"]])
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
                       "CLAUDE_PLUGIN_ROOT": str(root), "PLUGIN_ROOT": str(root),
                       "AGENT_STATE_DIRECTORY": str(self.state),
                       **isolated_machine(self.home),
                       "PATH": os.pathsep.join(entry for entry in os.environ.get("PATH", "").split(os.pathsep)
                                               if entry and not any((Path(entry) / name).exists()
                                                                    for name in ("tj_hook", "tj_hook.exe")))}
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--host", host],
                                input=json.dumps({"hook_event_name": event}), capture_output=True, text=True,
                                env=environment, timeout=30, check=True)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def test_a_current_session_runs_unchanged(self):
        self.registry(PIN)
        self.codex_build("aaaaaaaaaaaa")
        self.assertIsNone(self.run_hook("claude", "UserPromptSubmit"))
        self.assertIsNone(self.run_hook("codex", "UserPromptSubmit"))

    def test_a_stale_install_without_tj_hook_says_how_to_refresh(self):
        self.registry(OLD)
        output = self.run_hook("claude", "UserPromptSubmit")
        self.assertEqual({"decision": "block",
                          "reason": "tj-agents plugins are behind and tj_hook is missing; run agent:refresh-plugins."},
                         output)

    def test_a_running_refresh_is_not_started_again(self):
        self.registry(OLD)
        self.state.mkdir()
        (self.state / "plugin-refresh.lock").write_text("1", encoding="utf-8")
        self.assertEqual(UPDATING_CLAUDE, self.run_hook("claude", "UserPromptSubmit")["reason"])

    def test_a_recent_failed_refresh_is_reported_instead_of_retried(self):
        self.registry(OLD)
        write(self.state / "plugin-refresh.json", {"finished": time.time(), "failures": ["claude: list: boom"]})
        reason = self.run_hook("claude", "UserPromptSubmit")["reason"]
        self.assertEqual("tj-agents plugin refresh failed (claude: list: boom); run agent:refresh-plugins.", reason)

    def test_a_stale_codex_build_blocks_and_session_start_says_so(self):
        self.registry(PIN)
        self.codex_build("bbbbbbbbbbbb")
        self.state.mkdir()
        (self.state / "plugin-refresh.lock").write_text("1", encoding="utf-8")
        self.assertEqual(UPDATING_CODEX, self.run_hook("codex", "UserPromptSubmit")["reason"])
        context = self.run_hook("codex", "SessionStart")["hookSpecificOutput"]
        self.assertEqual({"hookEventName": "SessionStart", "additionalContext": UPDATING_CODEX}, context)

    def test_only_the_running_session_behind_asks_for_a_reload(self):
        self.registry(PIN)
        running = self.claude / "plugins/cache/tj-agents/agent" / f"{OLD[:12]}-d086935e"
        running.mkdir(parents=True)
        output = self.run_hook("claude", "UserPromptSubmit", running)
        self.assertEqual({"decision": "block", "reason": "Plugins updated: type /reload-plugins."}, output)
        current = self.claude / "plugins/cache/tj-agents/agent" / f"{PIN[:12]}-d086935e"
        current.mkdir(parents=True)
        self.assertIsNone(self.run_hook("claude", "UserPromptSubmit", current))

    def test_only_the_running_codex_session_behind_asks_for_a_restart(self):
        self.registry(PIN)
        self.codex_build("aaaaaaaaaaaa")
        older = self.home / "running-codex"
        write(older / ".codex-plugin/plugin.json", {"name": "agent", "version": "cccccccccccc"})
        expected = "Plugins updated: restart this session."
        self.assertEqual(expected, self.run_hook("codex", "UserPromptSubmit", older)["reason"])
        self.assertEqual(expected, self.run_hook("codex", "UserPromptSubmit", self.home / "deleted-build")["reason"])



class StartRefreshTests(unittest.TestCase):
    def test_the_refresh_starts_detached_and_hidden_through_the_shared_process_boundary(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import latest_plugins

        with tempfile.TemporaryDirectory() as state, \
                mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": state}), \
                mock.patch.object(latest_plugins.process, "start") as start:
            self.assertTrue(latest_plugins.start_refresh())
        command = start.call_args.args[0]
        self.assertEqual([sys.executable, "-B"], command[:2])
        self.assertTrue(command[2].endswith("refresh.py"))
        options = start.call_args.kwargs
        self.assertEqual(subprocess.DEVNULL, options["stdin"])
        if os.name == "nt":
            self.assertEqual(subprocess.CREATE_NEW_PROCESS_GROUP, options["creationflags"])
        else:
            self.assertTrue(options["start_new_session"])

    def test_a_missing_tj_hook_is_reported_not_raised(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import latest_plugins

        with tempfile.TemporaryDirectory() as state, \
                mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": state}), \
                mock.patch.object(latest_plugins.process, "start", side_effect=FileNotFoundError("tj_hook")):
            self.assertFalse(latest_plugins.start_refresh())


def run_tool_check(home: Path) -> str:
    state = home / "state"
    state.mkdir(exist_ok=True)
    (state / "plugin-refresh.lock").write_text("1", encoding="utf-8")
    environment = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": str(home / "claude"),
        "CODEX_HOME": str(home / "codex"),
        "AGENT_STATE_DIRECTORY": str(state),
        **isolated_machine(home),
    }
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--host", "claude"],
        input=json.dumps({"hook_event_name": "UserPromptSubmit"}),
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=True,
    )
    output = json.loads(result.stdout)
    assert output["decision"] == "block"
    return str(output["reason"])


def test_a_drifted_tj_item_receipt_blocks_and_names_the_drift(tmp_path: Path) -> None:
    write_receipt(tmp_path / "uv-tools", [{**PINS["tool"], "revision": OLD}])
    tool = PINS["tool"]
    assert run_tool_check(tmp_path) == (
        f"Updating tj-agents tools (tj-work-items is {tool['repository']}@{OLD[:12]}"
        f", pinned {tool['repository']}@{tool['revision'][:12]})… "
        "send the prompt again when this says done."
    )


def test_a_selected_profile_missing_from_the_receipt_blocks(tmp_path: Path) -> None:
    write_receipt(tmp_path / "uv-tools", [PINS["tool"]])
    plugin, pin = next(iter(PINS["profiles"].items()))
    name, _, marketplace = plugin.partition("@")
    cache = tmp_path / "codex" / "plugins" / "cache"
    (cache / marketplace / name / "local").mkdir(parents=True)
    assert f"{pin['package']} is not installed" in run_tool_check(tmp_path)


if __name__ == "__main__":
    unittest.main()
