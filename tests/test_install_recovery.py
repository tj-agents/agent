"""Launch recovery runs only the failing install step, and native dependency installs persist in order."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/agent/.agents/agent/runtime"
sys.path.insert(0, str(SCRIPTS))
from claude import plugins as CLAUDE  # noqa: E402
from host import Host  # noqa: E402
import install  # noqa: E402
import plugin_refresh  # noqa: E402
import plugin_autoupdate  # noqa: E402
import reload_plugins  # noqa: E402
import required_plugins  # noqa: E402


def load(name):
    spec = importlib.util.spec_from_file_location("automatic_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = load("launch_check")
REFRESH = load("reload_plugins")
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOPIC = {"topic": "a" * 48}


class InstallRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="install recovery spaces é ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        self.project = self.root / "consumer"
        self.project.mkdir()
        environment = {**os.environ, "AGENT_STATE_DIRECTORY": str(self.state)}
        environment.pop(CHECK.INSTALL_RECOVERY, None)
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        stderr = contextlib.redirect_stderr(io.StringIO())
        stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)

    def configure_channel(self, *_):
        (self.state / "machine-events.json").write_text(json.dumps(TOPIC), encoding="utf-8")
        return ["channel configured"]

    def check(self, reconcile=lambda project: []):
        with mock.patch.object(CLAUDE, "reconcile", reconcile):
            return CHECK.check(self.project, fetch=lambda path: b"")

    def test_an_unconfigured_channel_repairs_only_the_channel_step_then_rechecks(self):
        with mock.patch.object(install, "repair", side_effect=self.configure_channel) as repair:
            self.assertEqual("current", self.check())
        repair.assert_called_once_with("event channel and publishers", self.project)

    def test_a_failed_refresh_repairs_the_catch_up_step_once_before_retrying(self):
        self.configure_channel()
        repaired = []

        def reconcile(project):
            if not repaired:
                raise RuntimeError("outdated native installation")
            return []

        with mock.patch.object(install, "repair", side_effect=lambda step, project: repaired.append(step) or []):
            self.assertEqual("current", self.check(reconcile))
        self.assertEqual(["plugin and merge-policy catch-up"], repaired)

    def test_a_refresh_that_stays_failed_after_repair_reports_failure_without_looping(self):
        self.configure_channel()

        def reconcile(project):
            raise RuntimeError("still broken")

        with mock.patch.object(install, "repair", return_value=[]) as repair:
            self.assertEqual("failed", self.check(reconcile))
        repair.assert_called_once()

    def test_a_failed_install_step_stays_closed(self):
        with mock.patch.object(install, "repair", side_effect=install.InstallError("requires native credential enrollment")) as repair, \
                self.assertRaisesRegex(RuntimeError, "requires native credential enrollment"):
            self.check()
        repair.assert_called_once()
        self.assertFalse((self.state / "machine-events.json").exists())

    def test_recovery_marks_the_repair_and_clears_the_marker_afterwards(self):
        seen = []

        def repair(step, project):
            seen.append(os.environ.get(CHECK.INSTALL_RECOVERY))
            return self.configure_channel()

        with mock.patch.object(install, "repair", side_effect=repair):
            self.check()
        self.assertEqual(["1"], seen)
        self.assertNotIn(CHECK.INSTALL_RECOVERY, os.environ)

    def test_a_nested_launch_check_never_recovers_again(self):
        with mock.patch.dict(os.environ, {CHECK.INSTALL_RECOVERY: "1"}), \
                mock.patch.object(install, "repair") as repair:
            self.assertEqual("unconfigured", self.check())
        repair.assert_not_called()

    def test_concurrent_recovery_is_refused(self):
        lock = plugin_autoupdate.acquire_lock(self.state / "install-recovery.lock")
        self.addCleanup(lock.close)
        with mock.patch.object(install, "repair") as repair, \
                self.assertRaisesRegex(RuntimeError, "already running"):
            self.check()
        repair.assert_not_called()

    def test_missing_required_plugins_repair_the_plugin_setup_step(self):
        missing = required_plugins.MissingRequiredPluginsError("missing", self.project, ["agent@tj-agents"])
        with mock.patch.object(required_plugins, "check", side_effect=[missing, {}]) as check, \
                mock.patch.object(reload_plugins, "repair_missing", side_effect=RuntimeError("native install failed")), \
                mock.patch.object(install, "repair", return_value=[]) as repair:
            self.assertEqual((self.project, {}), CHECK.prepare_launch_roots(self.project, Host.CLAUDE, "native", []))
        self.assertEqual(2, check.call_count)
        repair.assert_called_once_with("plugin setup", self.project)

    def test_failed_plugin_setup_recovery_reports_the_original_refresh_failure(self):
        failure = reload_plugins.RefreshError("plugin install template@tj-agents: native install failed")
        with mock.patch.object(reload_plugins, "prepare_required", side_effect=failure) as prepare, \
                mock.patch.object(CHECK, "recover_install", side_effect=install.InstallError("machine setup is stale")) as recover, \
                self.assertRaisesRegex(RuntimeError, "plugin install template@tj-agents: native install failed") as raised:
            CHECK.prepare_launch_roots(self.project, Host.CLAUDE, "native", [])
        self.assertIn("Automatic plugin setup recovery failed: machine setup is stale", str(raised.exception))
        prepare.assert_called_once_with(self.project, Host.CLAUDE, "native", ())
        recover.assert_called_once_with(self.project, "plugin setup")

    def test_failed_plugin_setup_recovery_reports_the_original_os_error(self):
        failure = OSError("native socket closed")
        with mock.patch.object(reload_plugins, "prepare_required", side_effect=failure) as prepare, \
                mock.patch.object(CHECK, "recover_install", side_effect=RuntimeError("machine setup is stale")) as recover, \
                self.assertRaisesRegex(RuntimeError, "native socket closed") as raised:
            CHECK.prepare_launch_roots(self.project, Host.CLAUDE, "native", [])
        self.assertIn("Automatic plugin setup recovery failed: machine setup is stale", str(raised.exception))
        prepare.assert_called_once_with(self.project, Host.CLAUDE, "native", ())
        recover.assert_called_once_with(self.project, "plugin setup")

    def test_invalid_launch_input_never_runs_install_recovery(self):
        with mock.patch.object(CHECK, "recover_install") as recover:
            with self.assertRaises(REFRESH.required_plugins.RequiredPluginsError):
                CHECK.prepare_launch_roots(self.project, Host.CODEX, "native", ["--config"])
        recover.assert_not_called()


class DependencyInstallationIntegration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="plugin installs spaces é ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "claude"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home), "TJ_AGENTS_INSTALL_RECOVERY": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        roster = {"plugins": [{"name": "personal", "dependencies": ["agent"]},
                              {"name": "agent", "dependencies": ["base"]}, {"name": "base"}]}
        (self.home / "plugins.json").write_text(json.dumps(roster), encoding="utf-8")
        market = self.home / "plugins/known_marketplaces.json"
        market.parent.mkdir()
        market.write_text(json.dumps({"tj-agents": {"source": {"source": "github", "repo": "tj-agents/marketplace"},
                                                   "installLocation": str(self.home)}}), encoding="utf-8")
        (self.home / ".claude-plugin").mkdir()
        (self.home / ".claude-plugin/marketplace.json").write_text(json.dumps(roster), encoding="utf-8")
        native_run = plugin_refresh.run
        patcher = mock.patch.object(plugin_refresh, "run", side_effect=lambda command, project: native_run(
            [sys.executable, str(FIXTURES / "plugin_cli.py"), *command[1:]], project))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_shared_admission_installs_and_reads_back_a_new_project_declaration(self):
        identity = "agent@tj-agents"
        for directory in (".agents", ".claude"):
            (self.root / directory).mkdir()
        (self.root / ".agents/plugins.json").write_text(
            json.dumps({"schema_version": 1, "plugins": [identity]}), encoding="utf-8")
        (self.root / ".claude/settings.json").write_text(
            json.dumps({"enabledPlugins": {identity: True}}), encoding="utf-8")

        def inventory(_binary, _host, project, _options, _marketplace=None, **_kwargs):
            return json.loads(plugin_refresh.run(["fixture", "plugin", "list", "--json"], project))

        updated = set()
        with mock.patch.object(required_plugins, "inventory", side_effect=inventory):
            roots = REFRESH.prepare_required(self.root, Host.CLAUDE, "fixture", updated=updated)
        self.assertEqual({identity: self.home / "payloads/agent"}, roots)
        self.assertEqual({identity}, updated)
        self.assertEqual([["install", "base@tj-agents"], ["install", identity]],
                         [json.loads(line) for line in (self.home / "operations.jsonl").read_text(encoding="utf-8").splitlines()])
        records = json.loads((self.home / "plugins/installed_plugins.json").read_text(encoding="utf-8"))
        self.assertEqual("project", records["plugins"][identity][0]["scope"])

    def test_fresh_dependencies_persist_before_installing_the_dependent(self):
        CLAUDE.apply("fixture", self.root, [("personal@tj-agents", "project")], [])
        self.assertEqual([["install", name + "@tj-agents"] for name in ("base", "agent", "personal")],
                         [json.loads(line) for line in (self.home / "operations.jsonl").read_text().splitlines()])
        self.assertEqual([], CLAUDE.dependency_installs([("personal@tj-agents", "project", self.root)]))

    def test_outdated_dependent_is_updated_only_after_dependencies_persist(self):
        path = self.home / "plugins/installed_plugins.json"
        path.write_text(json.dumps({"plugins": {"personal@tj-agents": [{"scope": "project", "projectPath": str(self.root),
                                                                    "enabled": True, "version": "old"}]}}), encoding="utf-8")
        CLAUDE.apply("fixture", self.root, [], [("personal@tj-agents", "project", self.root)])
        self.assertEqual([["install", "base@tj-agents"], ["install", "agent@tj-agents"], ["update", "personal@tj-agents"]],
                         [json.loads(line) for line in (self.home / "operations.jsonl").read_text().splitlines()])

    def test_dependency_install_that_does_not_persist_fails(self):
        with mock.patch.dict(os.environ, {"FIXTURE_DROP_INSTALL": "agent@tj-agents"}), \
                self.assertRaisesRegex(REFRESH.RefreshError, "missing dependencies"):
            CLAUDE.apply("fixture", self.root, [("personal@tj-agents", "project")], [])

    def test_successful_native_response_without_durable_dependency_registration_fails(self):
        with mock.patch.dict(os.environ, {"FIXTURE_DROP_INSTALL": "agent@tj-agents", "FIXTURE_IGNORE_DEPENDENCIES": "1"}), \
                self.assertRaisesRegex(REFRESH.RefreshError, "agent@tj-agents.*did not persist enabled"):
            CLAUDE.apply("fixture", self.root, [("personal@tj-agents", "project")], [])

    def test_dependency_cycle_fails_before_starting_native_installation(self):
        dependencies = {"personal@tj-agents": ["agent@tj-agents"], "agent@tj-agents": ["personal@tj-agents"]}
        with self.assertRaisesRegex(REFRESH.RefreshError, "Cyclic plugin dependency"):
            CLAUDE.missing_dependencies("personal@tj-agents", "project", self.root, dependencies, [])
        self.assertFalse((self.home / "operations.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
