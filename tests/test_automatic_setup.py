"""Cross-platform integration: remote Git bootstrap, supported setup and durable native CLI state."""

import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents/agent/runtime"
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location("automatic_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = load("launch_check")
REFRESH = load("reload_plugins")
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class AutomaticSetupIntegration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="automatic setup spaces é ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.project = self.root / "consumer"
        self.project.mkdir()
        self.remote = self.root / "publisher"
        self.remote.mkdir()
        environment = {**os.environ, "AGENT_STATE_DIRECTORY": str(self.state), "GIT_CONFIG_COUNT": "1",
                       "GIT_CONFIG_KEY_0": "url." + self.remote.as_uri() + ".insteadOf",
                       "GIT_CONFIG_VALUE_0": "https://github.com/tj-agents/core.git"}
        for name in (CHECK.SETUP_RECOVERY, "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
            environment.pop(name, None)
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.test")
        shutil.copyfile(FIXTURES / "setup_probe.py", self.remote / "setup_probe.py")
        python = "'" + sys.executable.replace("'", "''") + "'"
        (self.remote / "setup.ps1").write_text(
            f"& {python} -B (Join-Path $PSScriptRoot 'setup_probe.py') @args\nexit $LASTEXITCODE\n", encoding="utf-8")
        (self.remote / "setup.sh").write_text(
            '#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -B "$(dirname "$0")/setup_probe.py" "$@"\n', encoding="utf-8")
        self.publish("first")

    def git(self, *arguments):
        result = subprocess.run(["git", "-C", str(self.remote), *arguments], capture_output=True,
                                text=True, encoding="utf-8", timeout=30, check=True)
        return result.stdout.strip()

    def publish(self, version):
        (self.remote / "revision").write_text(version, encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", version)

    def check(self, reconcile=lambda project: []):
        return CHECK.check(self.project, fetch=lambda path: b"", reconcile=reconcile)

    def test_fresh_machine_bootstraps_published_main_and_setup_is_idempotent(self):
        self.assertFalse((self.state / "sources/core").exists())
        self.assertEqual("current", self.check())
        installed = self.state / "installed-version"
        before = {name: (self.state / name).stat().st_mtime_ns
                  for name in ("installed-version", "setup-applications", "machine-events.json")}
        CHECK.bootstrap_setup(self.project)
        self.assertEqual("first", installed.read_text())
        self.assertEqual(before, {name: (self.state / name).stat().st_mtime_ns for name in before})
        self.assertEqual("1", (self.state / "setup-applications").read_text())

    def test_outdated_install_fetches_remote_main_before_recovering_failed_refresh(self):
        self.check()
        self.publish("published-new")

        def reconcile(project):
            if (self.state / "installed-version").read_text() != "published-new":
                raise RuntimeError("outdated native installation")
            return []

        self.assertEqual("current", self.check(reconcile))
        self.assertEqual("published-new", (self.state / "installed-version").read_text())
        self.assertEqual("2", (self.state / "setup-applications").read_text())

    def test_setup_failure_stays_closed_without_onboarding_or_retry_loop(self):
        (self.remote / "refuse").write_text("credential enrollment", encoding="utf-8")
        self.publish("refused")
        with self.assertRaisesRegex(RuntimeError, "requires native credential enrollment"):
            self.check()
        self.assertFalse((self.state / "installed-version").exists())

    def test_recovery_never_overwrites_a_dirty_managed_checkout(self):
        self.check()
        (self.state / "sources/core/revision").write_text("local changes", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "local changes"):
            CHECK.bootstrap_setup(self.project)


class DependencyInstallationIntegration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="plugin installs spaces é ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "claude"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home), "TJ_AGENTS_SETUP_RECOVERY": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        catalog = {"plugins": [{"name": "personal", "dependencies": ["agent"]},
                               {"name": "agent", "dependencies": ["base"]}, {"name": "base"}]}
        (self.home / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        market = self.home / "plugins/known_marketplaces.json"
        market.parent.mkdir()
        market.write_text(json.dumps({"tj-agents": {"source": {"source": "github", "repo": "tj-agents/marketplace"},
                                                   "installLocation": str(self.home)}}), encoding="utf-8")
        (self.home / ".claude-plugin").mkdir()
        (self.home / ".claude-plugin/marketplace.json").write_text(json.dumps(catalog), encoding="utf-8")
        native_run = REFRESH.run
        patcher = mock.patch.object(REFRESH, "run", side_effect=lambda command, project: native_run(
            [sys.executable, str(FIXTURES / "plugin_cli.py"), *command[1:]], project))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fresh_dependencies_persist_before_installing_the_dependent(self):
        REFRESH.apply_claude("fixture", self.root, [("personal@tj-agents", "project")], [])
        self.assertEqual([["install", name + "@tj-agents"] for name in ("base", "agent", "personal")],
                         [json.loads(line) for line in (self.home / "operations.jsonl").read_text().splitlines()])
        self.assertEqual([], REFRESH.dependency_installs([("personal@tj-agents", "project", self.root)]))

    def test_outdated_dependent_is_updated_only_after_dependencies_persist(self):
        path = self.home / "plugins/installed_plugins.json"
        path.write_text(json.dumps({"plugins": {"personal@tj-agents": [{"scope": "project", "projectPath": str(self.root),
                                                                    "enabled": True, "version": "old"}]}}), encoding="utf-8")
        REFRESH.apply_claude("fixture", self.root, [], [("personal@tj-agents", "project", self.root)])
        self.assertEqual([["install", "base@tj-agents"], ["install", "agent@tj-agents"], ["update", "personal@tj-agents"]],
                         [json.loads(line) for line in (self.home / "operations.jsonl").read_text().splitlines()])

    def test_dependency_install_that_does_not_persist_fails(self):
        with mock.patch.dict(os.environ, {"FIXTURE_DROP_INSTALL": "agent@tj-agents"}), \
                self.assertRaisesRegex(REFRESH.RefreshError, "missing dependencies"):
            REFRESH.apply_claude("fixture", self.root, [("personal@tj-agents", "project")], [])

    def test_successful_native_response_without_durable_dependency_registration_fails(self):
        with mock.patch.dict(os.environ, {"FIXTURE_DROP_INSTALL": "agent@tj-agents", "FIXTURE_IGNORE_DEPENDENCIES": "1"}), \
                self.assertRaisesRegex(REFRESH.RefreshError, "agent@tj-agents.*did not persist enabled"):
            REFRESH.apply_claude("fixture", self.root, [("personal@tj-agents", "project")], [])

    def test_dependency_cycle_fails_before_starting_native_installation(self):
        dependencies = {"personal@tj-agents": ["agent@tj-agents"], "agent@tj-agents": ["personal@tj-agents"]}
        with self.assertRaisesRegex(REFRESH.RefreshError, "Cyclic plugin dependency"):
            REFRESH.missing_dependencies("personal@tj-agents", "project", self.root, dependencies, [])
        self.assertFalse((self.home / "operations.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
