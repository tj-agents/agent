from __future__ import annotations

import importlib.util
import json
import re
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".agents/agent/runtime"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("reload_plugins", SCRIPTS / "reload_plugins.py")
REFRESH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REFRESH)
SESSION_VARIABLES = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDE_CODE_CHILD_SESSION")
ISOLATION = []


def setUpModule():
    home = tempfile.TemporaryDirectory(prefix="sync plugins home ")
    ISOLATION.append(home)
    environment = {key: value for key, value in os.environ.items() if key not in SESSION_VARIABLES}
    environment.update(GIT_CONFIG_COUNT='0', CLAUDE_CONFIG_DIR=str(Path(home.name) / "claude"), CODEX_HOME=str(Path(home.name) / "codex"),
                       AGENT_STATE_DIRECTORY=str(Path(home.name) / "state"), TJ_AGENTS_INSTALL_RECOVERY="1")
    patcher = mock.patch.dict(os.environ, environment, clear=True)
    patcher.start()
    ISOLATION.append(patcher)


def tearDownModule():
    for item in reversed(ISOLATION):
        (item.stop if hasattr(item, "stop") else item.cleanup)()


def updated(version="v2", outcome="updated", **extra):
    return json.dumps({"command": "update", "outcome": "ok", "updateOutcome": outcome, "newVersion": version, **extra})


class FakeClaude:
    """A claude CLI double whose update writes the reported version into its own installation records."""

    def __init__(self, installations, persist=True, version="v2", errors=None):
        self.installations = installations
        self.persist = persist
        self.version = version
        self.errors = errors or {}
        self.commands = []

    def __call__(self, command, cwd):
        self.commands.append((command, Path(cwd)))
        if command[1:3] == ["plugin", "list"]:
            return json.dumps([{**entry, "errors": self.errors.get(entry["id"], [])} for entry in self.installations])
        if command[1:3] == ["plugin", "install"] and self.persist:
            identity, scope = command[3], command[5]
            self.installations.append({"id": identity, "scope": scope, "projectPath": str(cwd),
                                       "enabled": True, "version": self.version})
        if command[1:3] == ["plugin", "update"]:
            identity, scope = command[3], command[5]
            for entry in self.installations:
                if (self.persist and entry["id"] == identity and entry["scope"] == scope
                        and (scope == "user" or REFRESH.path_key(entry["projectPath"]) == REFRESH.path_key(cwd))):
                    entry["version"] = self.version
            return "Checking for updates\n" + updated(self.version)
        return ""


class RefreshRunTests(unittest.TestCase):
    def test_native_failure_includes_the_command_identity(self):
        command = ["claude", "plugin", "install", "template@tj-agents"]
        failed = subprocess.CompletedProcess(command, 1, stdout="", stderr="native install failed")
        with mock.patch.object(REFRESH.subprocess, "run", return_value=failed), \
                mock.patch("sys.stderr"), \
                self.assertRaisesRegex(REFRESH.RefreshError,
                                       "plugin install template@tj-agents: native install failed"):
            REFRESH.run(command, ROOT)


class RefreshCliTests(unittest.TestCase):
    def setUp(self):
        caller = mock.patch.object(REFRESH, "calling_host", return_value="codex")
        caller.start()
        self.addCleanup(caller.stop)
        admission = mock.patch.object(REFRESH, "prepare_required", return_value=None)
        self.admission = admission.start()
        self.addCleanup(admission.stop)
        readiness = mock.patch.object(REFRESH.launch_check, "ensure_hooks")
        self.readiness = readiness.start()
        self.addCleanup(readiness.stop)

        for method in ("check_setup", "converge_setup"):
            patcher = mock.patch.object(REFRESH.required_plugins, method, return_value=[])
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_full_refresh_still_attempts_codex_after_claude_admission_fails(self):
        self.admission.side_effect = [REFRESH.RefreshError("claude install failed"), None]
        with mock.patch.object(REFRESH, "executable_for", return_value="native"), \
                mock.patch.object(REFRESH, "selection", return_value=({}, [])), \
                mock.patch.object(REFRESH, "refresh", return_value=[]) as refresh, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(1, REFRESH.main(["--all"]))
        self.assertEqual(["claude", "codex"], [call.args[1] for call in self.admission.call_args_list])
        refresh.assert_called_once_with("codex", "native", Path.cwd().resolve(), {}, [])

    def test_partial_admission_refreshes_the_healthy_host_without_consuming_events(self):
        for failed_host, healthy_host in (("claude", "codex"), ("codex", "claude")):
            def admit(project, host, **kwargs):
                if host == failed_host:
                    raise REFRESH.RefreshError(f"{host} install failed")

            self.admission.reset_mock()
            self.admission.side_effect = admit
            self.readiness.reset_mock()
            with self.subTest(failed_host=failed_host), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                cursor = state / "machine-events-cursor.json"
                saved = json.dumps({"since": "old", "since_at": 0})
                cursor.write_text(saved, encoding="utf-8")
                with mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(state)}), \
                        mock.patch.object(REFRESH.launch_check, "check", side_effect=AssertionError("event update")) as check, \
                        mock.patch.object(REFRESH, "executable_for", return_value="native"), \
                        mock.patch.object(REFRESH, "selection", return_value=({}, ["base@tj-agents"])), \
                        mock.patch.object(REFRESH, "refresh", return_value=[]) as refresh, \
                        mock.patch.object(REFRESH.required_plugins, "converge_setup", return_value=[]) as setup, \
                        mock.patch.object(REFRESH, "calling_host", return_value="claude"), \
                        mock.patch.object(REFRESH, "relaunch_session", return_value=None) as relaunch, \
                        mock.patch("sys.stdout") as stdout, mock.patch("sys.stderr"):
                    self.assertEqual(1, REFRESH.main([]))
                self.assertEqual(["claude", "codex"],
                                 [call.args[1] for call in self.admission.call_args_list[:2]])
                refresh.assert_called_once_with(healthy_host, "native", Path.cwd().resolve(), {}, ["base@tj-agents"])
                self.assertEqual((healthy_host,), setup.call_args.args[1])
                self.assertEqual(healthy_host == "codex", self.readiness.called)
                self.assertEqual(healthy_host == "claude", relaunch.called)
                self.assertIn(f"{failed_host} install failed",
                              "".join(call.args[0] for call in stdout.write.call_args_list))
                check.assert_not_called()
                self.assertEqual(saved, cursor.read_text(encoding="utf-8"))

    def test_default_both_adopts_only_the_claude_caller(self):
        restart = mock.Mock()
        adopt = mock.Mock(return_value={"relaunch": "armed"})
        self.readiness.side_effect = REFRESH.codex_hook_trust.RestartRequired("restart")
        with mock.patch.object(REFRESH, "calling_host", return_value="claude"), \
                mock.patch.object(REFRESH.launch_check, "check", return_value="current"), \
                mock.patch.object(REFRESH, "relaunch_codex", restart), \
                mock.patch.object(REFRESH, "relaunch_session", adopt), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(0, REFRESH.main([]))
        self.readiness.assert_called_once_with(Path.cwd().resolve(), running_session=False)
        restart.assert_not_called()
        adopt.assert_called_once()

    def test_default_both_adopts_only_the_codex_caller(self):
        restart = mock.Mock(return_value={"relaunch": "armed"})
        adopt = mock.Mock()
        self.readiness.side_effect = REFRESH.codex_hook_trust.RestartRequired("restart")
        with mock.patch.object(REFRESH, "calling_host", return_value="codex"), \
                mock.patch.object(REFRESH.launch_check, "check", return_value="current"), \
                mock.patch.object(REFRESH, "relaunch_codex", restart), \
                mock.patch.object(REFRESH, "relaunch_session", adopt), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(0, REFRESH.main([]))
        self.readiness.assert_called_once_with(Path.cwd().resolve(), running_session=True)
        restart.assert_called_once()
        adopt.assert_not_called()

    def test_unknown_caller_never_claims_client_adoption(self):
        restart = mock.Mock()
        adopt = mock.Mock()
        with mock.patch.object(REFRESH, "calling_host", return_value=None), \
                mock.patch.object(REFRESH.launch_check, "check", return_value="current"), \
                mock.patch.object(REFRESH, "relaunch_codex", restart), \
                mock.patch.object(REFRESH, "relaunch_session", adopt), \
                mock.patch("sys.stdout") as stdout, mock.patch("sys.stderr"):
            self.assertEqual(0, REFRESH.main([]))
        self.readiness.assert_called_once_with(Path.cwd().resolve(), running_session=False)
        restart.assert_not_called()
        adopt.assert_not_called()
        self.assertNotIn('"relaunch"', "".join(call.args[0] for call in stdout.write.call_args_list))

    def test_default_refresh_stops_when_neither_host_is_admitted(self):
        self.admission.side_effect = REFRESH.RefreshError("inventory failed")
        with mock.patch.object(REFRESH.launch_check, "check") as check, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(1, REFRESH.main([]))
        self.assertEqual(2, self.admission.call_count)
        check.assert_not_called()
        self.readiness.assert_not_called()
        REFRESH.required_plugins.converge_setup.assert_not_called()

    def test_verify_only_never_repairs_missing_installations(self):
        with mock.patch.object(REFRESH, "executable_for", return_value="native"), \
                mock.patch.object(REFRESH, "selection", return_value=({}, [])), \
                mock.patch.object(REFRESH, "verify", return_value={"host": "codex", "problems": []}), \
                mock.patch.object(REFRESH, "refresh") as refresh, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(0, REFRESH.main(["--host", "codex", "--verify-only"]))
        self.admission.assert_not_called()
        refresh.assert_not_called()

    def test_current_plugins_still_require_live_hook_reload(self):
        with mock.patch.object(REFRESH.launch_check, "check", return_value="current"), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(0, REFRESH.main(["--host", "codex"]))
        self.readiness.assert_called_once_with(Path.cwd().resolve(), running_session=True)

    def test_live_hook_reload_failure_fails_current_refresh(self):
        self.readiness.side_effect = RuntimeError("Live hooks still modified")
        with mock.patch.object(REFRESH.launch_check, "check", return_value="current"), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(1, REFRESH.main(["--host", "codex"]))

    def test_live_hook_reload_failure_fails_verified_refresh(self):
        self.readiness.side_effect = RuntimeError("Live hooks still modified")
        with mock.patch.object(REFRESH.agent_cli, "resolve_codex_executable", return_value=("codex", "version")), \
                mock.patch.object(REFRESH, "selection", return_value=({}, [])), \
                mock.patch.object(REFRESH, "refresh", return_value=[]), \
                mock.patch.object(REFRESH, "verify", return_value={"problems": []}), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(1, REFRESH.main(["--host", "codex", "--verify"]))
        self.readiness.assert_called_once_with(Path.cwd().resolve(), "codex", running_session=True)

    def test_default_uses_native_commands_without_verification_or_bound_launch(self):
        with mock.patch.object(REFRESH.agent_cli, "resolve_claude_executable", return_value="claude"), \
                mock.patch.object(REFRESH, "selection", return_value=({"tj-agents": "tj-agents/marketplace"}, ["workflow@tj-agents"])), \
                mock.patch.object(REFRESH, "verify") as verify, \
                mock.patch.object(REFRESH, "read_json", return_value={"tj-agents": {}}), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            fake = FakeClaude([{"id": "workflow@tj-agents", "scope": "user", "enabled": True, "version": "v1"}])
            with mock.patch.object(REFRESH, "run", side_effect=fake):
                self.assertEqual(0, REFRESH.main(["--host", "claude", "--all"]))
        self.assertEqual([command for command, _ in fake.commands], [
            ["claude", "plugin", "marketplace", "update", "tj-agents"],
            ["claude", "plugin", "list", "--json"],
            ["claude", "plugin", "update", "workflow@tj-agents", "--scope", "user", "--json"],
            ["claude", "plugin", "list", "--json"],
        ])
        verify.assert_not_called()

    def test_default_applies_published_events_and_falls_back_to_a_full_refresh_when_unconfigured(self):
        for result, code in (("current", 0), ("updated", 0), ("failed", 1), ("offline", 1)):
            with self.subTest(result=result), mock.patch.object(REFRESH.launch_check, "check", return_value=result), \
                    mock.patch.object(REFRESH, "refresh") as refresh, mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                self.assertEqual(code, REFRESH.main([]))
                refresh.assert_not_called()
        with mock.patch.object(REFRESH.launch_check, "check", return_value="unconfigured"), \
                mock.patch.object(REFRESH.agent_cli, "resolve_claude_executable", return_value="claude"), \
                mock.patch.object(REFRESH, "selection", return_value=({}, [])), \
                mock.patch.object(REFRESH, "refresh") as refresh, mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(0, REFRESH.main(["--host", "claude"]))
        refresh.assert_called_once()

    def test_a_codex_only_sync_passes_its_host_to_the_launch_check(self):
        with mock.patch.object(REFRESH.launch_check, "check", return_value="current") as check, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(0, REFRESH.main(["--host", "codex"]))
            self.assertEqual("codex", check.call_args.kwargs["host"])
            REFRESH.main([])
            self.assertIsNone(check.call_args.kwargs["host"])

    def test_current_packages_do_not_make_a_sync_successful_with_unready_hooks(self):
        self.readiness.side_effect = RuntimeError("required hook needs review")
        with mock.patch.object(REFRESH.launch_check, "check", return_value="current"), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(1, REFRESH.main(["--host", "codex"]))
        self.readiness.assert_called_once()
        REFRESH.required_plugins.converge_setup.assert_not_called()

    def test_default_early_return_converges_plugin_setup_before_success(self):
        with mock.patch.object(REFRESH.launch_check, "check", return_value="current"), \
                mock.patch.object(REFRESH.required_plugins, "converge_setup", return_value=[]) as converge, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(0, REFRESH.main(["--host", "codex"]))
        self.assertEqual((ROOT, ("codex",), {"codex": None}, False), converge.call_args.args)

    def test_failed_launch_or_refresh_never_applies_plugin_setup(self):
        with mock.patch.object(REFRESH.launch_check, "check", return_value="failed"), \
                mock.patch.object(REFRESH.required_plugins, "converge_setup") as converge, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(1, REFRESH.main(["--host", "codex"]))
        converge.assert_not_called()

        with mock.patch.object(REFRESH, "selection", return_value=({}, [])), \
                mock.patch.object(REFRESH, "refresh", side_effect=REFRESH.RefreshError("failed refresh")), \
                mock.patch.object(REFRESH.required_plugins, "converge_setup") as converge, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(1, REFRESH.main(["--host", "codex", "--all"]))
        converge.assert_not_called()

    def test_offline_event_still_checks_hook_readiness_and_allows_explicit_onboarding(self):
        with mock.patch.object(REFRESH.launch_check, "check", return_value="offline"), \
                mock.patch.object(REFRESH.required_plugins, "converge_setup", return_value=[]) as converge, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(1, REFRESH.main(["--host", "codex", "--onboard"]))
        self.readiness.assert_called_once()
        self.assertEqual((ROOT, ("codex",), {"codex": None}, True), converge.call_args.args)

    def test_offline_event_with_unready_hooks_blocks_setup(self):
        self.readiness.side_effect = RuntimeError("required hook needs review")
        with mock.patch.object(REFRESH.launch_check, "check", return_value="offline"), \
                mock.patch.object(REFRESH.required_plugins, "converge_setup") as converge, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(1, REFRESH.main(["--host", "codex", "--onboard"]))
        converge.assert_not_called()

    def test_verify_only_checks_plugin_setup_without_applying_or_onboarding(self):
        with mock.patch.object(REFRESH, "executable_for", return_value="codex"), \
                mock.patch.object(REFRESH, "selection", return_value=({}, [])), \
                mock.patch.object(REFRESH, "verify", return_value={"host": "codex", "plugins": [], "problems": []}), \
                mock.patch.object(REFRESH.required_plugins, "check_setup", return_value=[]) as check, \
                mock.patch.object(REFRESH.required_plugins, "converge_setup") as converge, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(0, REFRESH.main(["--host", "codex", "--verify-only"]))
        check.assert_called_once_with(ROOT, ("codex",), {"codex": None})
        converge.assert_not_called()

    def test_refresh_updates_applicable_claude_local_installations(self):
        fake = FakeClaude([
            {"id": "workflow@tj-agents", "scope": "local", "projectPath": str(ROOT), "enabled": True, "version": "v1"},
            {"id": "workflow@tj-agents", "scope": "local", "projectPath": str(ROOT / "other"), "enabled": True,
             "version": "v1"},
            {"id": "workflow@tj-agents", "scope": "user", "enabled": True, "version": "v1"}])
        with mock.patch.object(REFRESH, "read_json", return_value={}), mock.patch.object(REFRESH, "run", side_effect=fake):
            self.assertEqual(["workflow@tj-agents"], REFRESH.refresh("claude", "claude", ROOT, {}, ["workflow@tj-agents"]))
        self.assertEqual(fake.commands, [
            (["claude", "plugin", "list", "--json"], ROOT),
            (["claude", "plugin", "update", "workflow@tj-agents", "--scope", "local", "--json"], ROOT),
            (["claude", "plugin", "update", "workflow@tj-agents", "--scope", "user", "--json"], ROOT),
            (["claude", "plugin", "list", "--json"], ROOT),
        ])
        self.assertEqual("v1", fake.installations[1]["version"])

    def test_claude_installs_only_at_the_scope_the_repository_declares(self):
        for declared, scope in (("settings.local.json", "local"), ("settings.json", "project")):
            def settings(path, declared=declared):
                return {"enabledPlugins": {"infonetica@tj-agents": True}} if path.name == declared else {}
            fake = FakeClaude([])

            def install(command, cwd, fake=fake, scope=scope):
                if command[2] == "install":
                    fake.installations.append({"id": command[3], "scope": scope, "projectPath": str(cwd),
                                               "enabled": True, "version": "v1"})
                return fake(command, cwd)
            with self.subTest(declared=declared), mock.patch.object(REFRESH, "read_json", side_effect=settings), \
                    mock.patch.object(REFRESH, "run", side_effect=install):
                REFRESH.refresh("claude", "claude", ROOT, {}, ["infonetica@tj-agents"])
            self.assertEqual([command for command, _ in fake.commands], [
                ["claude", "plugin", "list", "--json"],
                ["claude", "plugin", "install", "infonetica@tj-agents", "--scope", scope],
                ["claude", "plugin", "update", "infonetica@tj-agents", "--scope", scope, "--json"],
                ["claude", "plugin", "list", "--json"],
            ])

    def test_a_project_declaration_still_updates_the_user_installation_every_other_directory_uses(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".claude").mkdir()
            (project / ".claude/settings.local.json").write_text(json.dumps({"enabledPlugins": {
                "workflow@tj-agents": True}}), encoding="utf-8")
            fake = FakeClaude([
                {"id": "workflow@tj-agents", "scope": "user", "enabled": True, "version": "v1"},
                {"id": "workflow@tj-agents", "scope": "local", "projectPath": str(project), "enabled": True,
                 "version": "v1"}])
            with mock.patch.object(REFRESH, "run", side_effect=fake):
                REFRESH.refresh("claude", "claude", project, {}, ["workflow@tj-agents"])
        self.assertIn(["claude", "plugin", "update", "workflow@tj-agents", "--scope", "user", "--json"],
                      [command for command, _ in fake.commands])
        self.assertEqual(["v2", "v2"], [entry["version"] for entry in fake.installations])

    def test_an_update_that_reports_success_but_does_not_persist_fails(self):
        fake = FakeClaude([{"id": "workflow@tj-agents", "scope": "user", "enabled": True, "version": "v1"}], persist=False)
        with mock.patch.object(REFRESH, "read_json", return_value={}), mock.patch.object(REFRESH, "run", side_effect=fake), \
                self.assertRaisesRegex(REFRESH.RefreshError, r"workflow@tj-agents \(user\) update did not persist.*v1.*v2"):
            REFRESH.refresh("claude", "claude", ROOT, {}, ["workflow@tj-agents"])

    def test_a_skipped_or_stale_catalog_update_is_a_failure_even_with_exit_code_zero(self):
        for output in (updated(outcome="skipped", message="Skipped - pinned"),
                       updated(outcome="up_to_date", refreshFailed=True), "Updated.\n"):
            def run(command, cwd, output=output):
                return output if command[2] == "update" else json.dumps(
                    [{"id": "workflow@tj-agents", "scope": "user", "enabled": True, "version": "v1"}])
            with self.subTest(output=output), mock.patch.object(REFRESH, "read_json", return_value={}), \
                    mock.patch.object(REFRESH, "run", side_effect=run), self.assertRaises(REFRESH.RefreshError):
                REFRESH.refresh("claude", "claude", ROOT, {}, ["workflow@tj-agents"])

    def test_listed_plugin_errors_fail_the_refresh_loudly(self):
        dependency = 'Dependency "work@work-agents" is not installed'
        fake = FakeClaude([
            {"id": "infonetica@tj-agents", "scope": "user", "enabled": True, "version": "v1"},
            {"id": "infonetica@tj-agents", "scope": "local", "projectPath": str(ROOT / "elsewhere"), "enabled": True,
             "version": "v1"}], errors={"infonetica@tj-agents": [dependency]})
        with mock.patch.object(REFRESH, "read_json", return_value={}), mock.patch.object(REFRESH, "run", side_effect=fake), \
                self.assertRaises(REFRESH.RefreshError) as raised:
            REFRESH.refresh("claude", "claude", ROOT, {}, ["infonetica@tj-agents"])
        self.assertEqual(f"infonetica@tj-agents (user): {dependency}", str(raised.exception))

    def test_main_reports_refresh_problems_on_stderr_and_fails(self):
        with mock.patch.object(REFRESH.agent_cli, "resolve_claude_executable", return_value="claude"), \
                mock.patch.object(REFRESH, "selection", return_value=({}, ["workflow@tj-agents"])), \
                mock.patch.object(REFRESH, "refresh", side_effect=REFRESH.RefreshError("workflow@tj-agents (user): broken")), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr") as stderr:
            self.assertEqual(1, REFRESH.main(["--host", "claude", "--all"]))
        self.assertIn("plugins: claude: workflow@tj-agents (user): broken",
                      "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_codex_refresh_removes_project_only_plugins_from_user_scope(self):
        with tempfile.TemporaryDirectory() as home:
            config = Path(home) / "config.toml"
            config.write_text('[plugins."base@tj-agents"]\nenabled = true\n', encoding="utf-8")

            def add(command, project):
                identity = command[-1]
                if command[2] == "add":
                    config.write_text(config.read_text(encoding="utf-8")
                                      + f'\n[plugins."{identity}"]\nenabled = true\n', encoding="utf-8")
                return ""
            with mock.patch.dict("os.environ", {"CODEX_HOME": home}), mock.patch.object(REFRESH, "run", side_effect=add):
                REFRESH.refresh("codex", "codex", ROOT, {}, ["base@tj-agents", "infonetica@tj-agents"])
            text = config.read_text(encoding="utf-8")
        self.assertIn('[plugins."base@tj-agents"]\nenabled = true', text)
        self.assertNotIn('infonetica@tj-agents', text)

    def test_missing_claude_marketplace_is_registered_before_refresh(self):
        with mock.patch.object(REFRESH, "read_json", return_value={}), \
                mock.patch.object(REFRESH, "run") as run:
            REFRESH.refresh("claude", "claude", ROOT, {"tj-agents": "tj-agents/marketplace"}, [])
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["claude", "plugin", "marketplace", "add", "tj-agents/marketplace"],
            ["claude", "plugin", "marketplace", "update", "tj-agents"],
        ])

    def test_project_selection_overrides_user_and_foreign_sources_are_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            for path in (root / "settings.json", project / ".claude/settings.json"):
                path.parent.mkdir(parents=True, exist_ok=True)
            (root / "settings.json").write_text(json.dumps({"enabledPlugins": {"base@tj-agents": True}}))
            (project / ".claude/settings.json").write_text(json.dumps({
                "enabledPlugins": {"base@tj-agents": False, "workflow@tj-agents": True, "other@foreign": True},
                "extraKnownMarketplaces": {
                    "tj-agents": {"source": {"source": "github", "repo": "tj-agents/marketplace"}},
                    "foreign": {"source": {"source": "github", "repo": "elsewhere/plugin"}},
                }}))
            with mock.patch.object(REFRESH, "configuration_root", return_value=root):
                sources, enabled = REFRESH.selection("claude", "unused", project)
            self.assertEqual({"tj-agents": "tj-agents/marketplace"}, sources)
            self.assertEqual(["workflow@tj-agents"], enabled)

    def test_verification_detects_stale_package_bytes_at_a_current_marketplace_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            market = root / "market"
            source = market / "plugins/base"
            install = root / "cache/base"
            for path in (market / ".claude-plugin", source, install, root / "plugins"):
                path.mkdir(parents=True, exist_ok=True)
            (source / "payload").write_bytes(b"current")
            (install / "payload").write_bytes(b"old")
            (market / ".claude-plugin/marketplace.json").write_text(json.dumps({
                "plugins": [{"name": "base", "source": "./plugins/base"}]}), encoding="utf-8")
            (root / "plugins/known_marketplaces.json").write_text(json.dumps({
                "tj-agents": {"installLocation": str(market)}}), encoding="utf-8")
            inventory = [{"id": "base@tj-agents", "enabled": True, "scope": "user", "installPath": str(install)}]
            with mock.patch.object(REFRESH, "configuration_root", return_value=root), \
                    mock.patch.object(REFRESH, "run", side_effect=[json.dumps(inventory), "a" * 40 + " refs/heads/main", "a" * 40]), \
                    mock.patch("sys.stderr"):
                report = REFRESH.verify("claude", "claude", root, {"tj-agents": "tj-agents/marketplace"}, ["base@tj-agents"])
            self.assertEqual(1, len(report["problems"]))
            self.assertIn("Native installed bytes differ", report["problems"][0])

    def test_verification_ignores_claude_in_use_markers_and_reports_listed_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            market = root / "market"
            source = market / "plugins/base"
            install = root / "cache/base"
            for path in (market / ".claude-plugin", source, install / ".in_use", root / "plugins"):
                path.mkdir(parents=True, exist_ok=True)
            (source / "payload").write_bytes(b"current")
            (install / "payload").write_bytes(b"current")
            (install / ".in_use" / "4242").write_text('{"pid":4242}', encoding="utf-8")
            (market / ".claude-plugin/marketplace.json").write_text(json.dumps({
                "plugins": [{"name": "base", "source": "./plugins/base"}]}), encoding="utf-8")
            (root / "plugins/known_marketplaces.json").write_text(json.dumps({
                "tj-agents": {"installLocation": str(market)}}), encoding="utf-8")
            for errors, expected in (([], []), (["broken hook"], ["base@tj-agents (user): broken hook"])):
                inventory = [{"id": "base@tj-agents", "enabled": True, "scope": "user", "installPath": str(install),
                              "errors": errors}]
                with self.subTest(errors=errors), mock.patch.object(REFRESH, "configuration_root", return_value=root), \
                        mock.patch.object(REFRESH, "run", side_effect=[json.dumps(inventory), "a" * 40 + " refs/heads/main",
                                                                       "a" * 40]), \
                        mock.patch("sys.stderr"):
                    report = REFRESH.verify("claude", "claude", root, {"tj-agents": "tj-agents/marketplace"},
                                            ["base@tj-agents"])
                self.assertEqual(expected, report["problems"])

    def test_supported_github_sources_are_normalized_before_selection(self):
        for source in ("ssh://git@github.com/TJ-Agents/core.git", "git@github.com:tj-agents/core.git",
                       "https://github.com/TJ-AGENTS/Core.git", "tj-agents/core"):
            with self.subTest(source=source):
                self.assertEqual("tj-agents/core", REFRESH.repository(source))
                config = {"marketplaces": {"core": {"source_type": "git", "source": source}},
                          "plugins": {"base@core": {"enabled": True}}}
                with mock.patch.object(REFRESH, "AppServer") as server:
                    server.return_value.__enter__.return_value.request.return_value = {"config": config}
                    with self.assertRaisesRegex(REFRESH.RefreshError, "published through the tj-agents marketplace"):
                        REFRESH.selection("codex", "codex", ROOT)

    def test_claude_refresh_rejects_python_and_react_producer_selections(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("python", "react"):
                with self.subTest(plugin=name):
                    config = {"enabledPlugins": {f"{name}@{name}-agents": True},
                              "extraKnownMarketplaces": {f"{name}-agents": {
                                  "source": {"source": "github", "repo": f"tj-agents/{name}"}}}}
                    with mock.patch.object(REFRESH, "read_json", return_value=config):
                        with self.assertRaisesRegex(REFRESH.RefreshError, "published through the tj-agents marketplace"):
                            REFRESH.selection("claude", "claude", root)

    def test_external_publisher_packages_are_verified_at_the_declared_revision(self):
        clean_git = {key: value for key, value in os.environ.items() if not key.startswith('GIT_CONFIG_')}
        environment = mock.patch.dict(os.environ, clean_git, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            publisher = root / "publisher"
            publisher.mkdir()
            subprocess.run(["git", "init", str(publisher)], check=True, capture_output=True)
            package = publisher / "plugins/base"
            package.mkdir(parents=True)
            (package / "payload").write_bytes(b"published")
            subprocess.run(["git", "-C", str(publisher), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(publisher), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "commit", "-m", "package"], check=True, capture_output=True)
            revision = subprocess.check_output(["git", "-C", str(publisher), "rev-parse", "HEAD"], text=True).strip()
            run_native = REFRESH.run

            def run(command, project):
                arguments = [str(publisher) if value == "https://github.com/tj-agents/core.git" else value for value in command]
                return run_native(arguments, project)

            clones = {}
            source = {"source": "git-subdir", "url": "ssh://git@github.com/TJ-Agents/core.git",
                      "path": "plugins/base", "sha": revision}
            with mock.patch.object(REFRESH, "run", side_effect=run) as commands, mock.patch("sys.stderr"):
                expected = REFRESH.source_tree(source, root, root, root / "scratch", clones)
                self.assertEqual({"payload": b"published"}, expected)
                self.assertEqual(expected, REFRESH.source_tree(source, root, root, root / "scratch", clones))
                self.assertEqual(1, sum("fetch" in call.args[0] for call in commands.call_args_list))
                source["path"] = "./../../escape"
                with self.assertRaisesRegex(REFRESH.RefreshError, "escapes"):
                    REFRESH.source_tree(source, root, root, root / "scratch", clones)

    def test_another_projects_installation_does_not_satisfy_verification(self):
        inventory = [{"id": "base@tj-agents", "enabled": True, "scope": "project", "projectPath": str(ROOT / "elsewhere")}]
        with mock.patch.object(REFRESH, "run", return_value=json.dumps(inventory)), mock.patch("sys.stderr"):
            report = REFRESH.verify("claude", "claude", ROOT, {}, ["base@tj-agents"])
        self.assertEqual(["Missing enabled native installation: base@tj-agents"], report["problems"])

    def test_verify_only_does_not_modify_installations(self):
        with mock.patch.object(REFRESH.agent_cli, "resolve_codex_executable", return_value=("codex", "version")), \
                mock.patch.object(REFRESH, "selection", return_value=({}, [])), \
                mock.patch.object(REFRESH, "refresh") as refresh, \
                mock.patch.object(REFRESH, "verify", return_value={"problems": []}) as verify, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(0, REFRESH.main(["--host", "codex", "--verify-only"]))
        refresh.assert_not_called()
        verify.assert_called_once()

    def test_native_failure_is_reported_and_other_host_is_still_attempted(self):
        with mock.patch.object(REFRESH.agent_cli, "resolve_claude_executable", return_value="claude"), \
                mock.patch.object(REFRESH.agent_cli, "resolve_codex_executable", return_value=("codex", "version")), \
                mock.patch.object(REFRESH, "selection", side_effect=[REFRESH.RefreshError("failed"), ({}, [])]), \
                mock.patch.object(REFRESH, "refresh") as refresh, mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(1, REFRESH.main([]))
        refresh.assert_called_once_with("codex", "codex", Path.cwd().resolve(), {}, [])


CORE_HEAD = "c" * 40
OLD_HEAD = "a" * 40
DOTNET_HEAD = "d" * 40
CATALOG = {"plugins": [
    {"name": "base", "source": {"source": "git-subdir", "url": "https://github.com/tj-agents/core.git",
                                "path": "./plugins/base", "ref": "main"}},
    {"name": "dotnet", "source": {"source": "git-subdir", "url": "https://github.com/tj-agents/dotnet.git",
                                  "path": "./plugins/dotnet", "ref": "main"}}]}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class ClaudeHome:
    """A CLAUDE_CONFIG_DIR with the tj-agents marketplace, its git-subdir catalog and recorded installs."""

    def __init__(self, test):
        temp = tempfile.TemporaryDirectory(prefix="claude home ")
        test.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / "claude"
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config),
                                               "AGENT_STATE_DIRECTORY": str(self.root / "state")})
        patcher.start()
        test.addCleanup(patcher.stop)
        write(self.config / "plugins/known_marketplaces.json", {"tj-agents": {
            "source": {"source": "github", "repo": "tj-agents/marketplace"}, "installLocation": str(self.root / "market")}})
        write(self.root / "market/.claude-plugin/marketplace.json", CATALOG)

    def project(self, name, settings="settings.local.json", *identities):
        directory = self.root / name
        write(directory / ".claude" / settings, {"enabledPlugins": {identity: True for identity in identities}})
        return directory

    def installed(self, *entries):
        plugins = {}
        for identity, scope, owner, sha in entries:
            entry = {"scope": scope, "version": "v1", "gitCommitSha": sha}
            if owner:
                entry["projectPath"] = str(owner)
            plugins.setdefault(identity, []).append(entry)
        write(self.config / "plugins/installed_plugins.json", {"version": 2, "plugins": plugins})
        return [{**entry, "id": identity, "enabled": True} for identity, entries in plugins.items() for entry in entries]


class HeadReconcileTests(unittest.TestCase):
    def test_missing_declared_plugin_is_installed_without_a_new_publish_event(self):
        project = self.home.project('new-project', 'settings.json', 'base@tj-agents')
        self.home.installed()
        calls = []

        def native(command, cwd):
            calls.append(command)
            if command[1:3] == ['plugin', 'list']:
                return json.dumps([{'id': 'base@tj-agents', 'scope': 'project',
                                    'projectPath': str(project), 'enabled': True, 'version': 'v1'}])
            return ''

        with mock.patch.object(REFRESH, 'run', side_effect=native):
            self.assertEqual(['base@tj-agents'], REFRESH.reconcile_claude(project))
        self.assertIn(['claude', 'plugin', 'install', 'base@tj-agents', '--scope', 'project'], calls)

    def test_an_update_first_installs_dependencies_the_catalog_newly_declares(self):
        project = self.home.project('core', 'settings.json', 'personal@tj-agents')
        write(self.home.root / "market/.claude-plugin/marketplace.json", {"plugins": [
            {"name": "personal", "dependencies": ["base", "agent"]},
            {"name": "agent", "dependencies": ["base", "cli"]}, {"name": "cli"}, {"name": "base"}]})
        fake = FakeClaude(self.home.installed(("personal@tj-agents", "project", project, CORE_HEAD),
                                              ("base@tj-agents", "user", None, CORE_HEAD)))
        with mock.patch.object(REFRESH, 'run', side_effect=fake):
            REFRESH.apply_claude('claude', project, [], [('personal@tj-agents', 'project', project)])
        self.assertEqual([command[2:6] for command, _ in fake.commands][:3], [
            ['install', 'cli@tj-agents', '--scope', 'project'],
            ['install', 'agent@tj-agents', '--scope', 'project'],
            ['update', 'personal@tj-agents', '--scope', 'project']])
        self.assertEqual({project}, {cwd for command, cwd in fake.commands if command[2] == 'install'})

    def test_a_current_plugin_still_gets_a_dependency_its_catalog_declares(self):
        project = self.home.project('core', 'settings.json', 'personal@tj-agents')
        write(self.home.root / "market/.claude-plugin/marketplace.json", {"plugins": [
            {"name": "personal", "dependencies": ["agent", "service@other"]}, {"name": "agent"}]})
        fake = FakeClaude(self.home.installed(("personal@tj-agents", "project", project, CORE_HEAD)))
        with mock.patch.object(REFRESH, 'run', side_effect=fake):
            self.assertEqual(['agent@tj-agents'], REFRESH.reconcile_claude(project))
        self.assertEqual([['install', 'agent@tj-agents', '--scope', 'project']],
                         [command[2:6] for command, _ in fake.commands if command[2] == 'install'])

    def test_a_dependency_from_another_marketplace_keeps_its_own_identity(self):
        self.assertEqual("service@other", REFRESH.dependency_identity({"name": "service", "marketplace": "other"},
                                                                      "tj-agents"))
        self.assertEqual("agent@tj-agents", REFRESH.dependency_identity("agent", "tj-agents"))
        self.assertIsNone(REFRESH.dependency_identity({"marketplace": "other"}, "tj-agents"))

    def test_missing_installation_that_does_not_persist_fails_refresh(self):
        project = self.home.project('new-project', 'settings.json', 'base@tj-agents')
        self.home.installed()
        with mock.patch.object(REFRESH, 'run', return_value='[]'), \
                self.assertRaisesRegex(REFRESH.RefreshError, 'did not persist enabled'):
            REFRESH.reconcile_claude(project)

    def test_repair_never_enables_a_plugin_excluded_from_native_selection(self):
        for host in ('claude', 'codex'):
            with self.subTest(host=host), mock.patch.object(REFRESH, 'executable_for', return_value='native'), \
                    mock.patch.object(REFRESH, 'selection', return_value=({}, [])), \
                    mock.patch.object(REFRESH, 'run') as native, \
                    self.assertRaisesRegex(REFRESH.RefreshError, 'not enabled'):
                REFRESH.repair_missing(host, self.home.root, ['base@tj-agents'])
            native.assert_not_called()

    def setUp(self):
        self.home = ClaudeHome(self)
        patcher = mock.patch.object(REFRESH.agent_cli, "resolve_claude_executable", return_value="claude")
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, heads, trees=()):
        trees = dict(trees)
        REFRESH.plugin_autoupdate.record_contents(heads, trees, {key.split("@")[1].split(":")[0] for key in trees})

    def test_installs_at_their_publisher_heads_start_no_process(self):
        project = self.home.project("preaward", "settings.local.json", "base@tj-agents", "dotnet@tj-agents")
        self.home.installed(("base@tj-agents", "local", project, CORE_HEAD),
                            ("dotnet@tj-agents", "local", project, DOTNET_HEAD))
        self.record({"tj-agents/core@main": CORE_HEAD, "tj-agents/dotnet@main": DOTNET_HEAD})
        with mock.patch.object(REFRESH, "run", side_effect=AssertionError("process started")):
            self.assertEqual([], REFRESH.reconcile_claude(project))

    def test_an_install_whose_files_are_unchanged_at_the_new_head_is_left_alone(self):
        project = self.home.project("preaward", "settings.local.json", "base@tj-agents", "dotnet@tj-agents")
        fake = FakeClaude(self.home.installed(("base@tj-agents", "local", project, OLD_HEAD),
                                              ("dotnet@tj-agents", "local", project, OLD_HEAD)))
        self.record({"tj-agents/core@main": CORE_HEAD, "tj-agents/dotnet@main": DOTNET_HEAD},
                    {f"tj-agents/core@{OLD_HEAD}:plugins/base": "1" * 40,
                     f"tj-agents/core@{CORE_HEAD}:plugins/base": "1" * 40,
                     f"tj-agents/dotnet@{OLD_HEAD}:plugins/dotnet": "2" * 40,
                     f"tj-agents/dotnet@{DOTNET_HEAD}:plugins/dotnet": "3" * 40})
        with mock.patch.object(REFRESH, "run", side_effect=fake):
            self.assertEqual(["dotnet@tj-agents"], REFRESH.reconcile_claude(project))
        self.assertEqual([
            ["claude", "plugin", "marketplace", "update", "tj-agents"],
            ["claude", "plugin", "update", "dotnet@tj-agents", "--scope", "local", "--json"],
            ["claude", "plugin", "list", "--json"],
        ], [command for command, _ in fake.commands])

    def test_an_install_at_an_unrecorded_commit_waits_for_a_publish_to_be_compared(self):
        project = self.home.project("preaward", "settings.local.json", "base@tj-agents")
        fake = FakeClaude(self.home.installed(("base@tj-agents", "local", project, OLD_HEAD)))
        self.record({"tj-agents/core@main": CORE_HEAD})
        with mock.patch.object(REFRESH, "run", side_effect=fake):
            self.assertEqual([], REFRESH.reconcile_claude(project))
            self.assertEqual([], fake.commands)
            REFRESH.reconcile_claude(project, event=True, repositories={"tj-agents/core"})
        self.assertIn(["claude", "plugin", "update", "base@tj-agents", "--scope", "local", "--json"],
                      [command for command, _ in fake.commands])

    def test_a_project_updates_only_its_own_stale_copy(self):
        current = self.home.project("authz", "settings.local.json", "base@tj-agents")
        stale = self.home.project("erm", "settings.local.json", "base@tj-agents")
        fake = FakeClaude(self.home.installed(("base@tj-agents", "local", current, CORE_HEAD),
                                              ("base@tj-agents", "local", stale, OLD_HEAD)))
        self.record({"tj-agents/core@main": CORE_HEAD}, {f"tj-agents/core@{OLD_HEAD}:plugins/base": "1" * 40,
                                                       f"tj-agents/core@{CORE_HEAD}:plugins/base": "2" * 40})
        with mock.patch.object(REFRESH, "run", side_effect=fake):
            self.assertEqual([], REFRESH.reconcile_claude(current))
            REFRESH.reconcile_claude(stale)
        self.assertEqual([(["claude", "plugin", "update", "base@tj-agents", "--scope", "local", "--json"], stale)],
                         [(command, cwd) for command, cwd in fake.commands if command[2] == "update"])

    def test_an_unknown_publisher_head_is_never_guessed_without_an_event(self):
        project = self.home.project("preaward", "settings.local.json", "base@tj-agents")
        self.home.installed(("base@tj-agents", "local", project, OLD_HEAD))
        with mock.patch.object(REFRESH, "run", side_effect=AssertionError("process started")):
            self.assertEqual([], REFRESH.reconcile_claude(project))

    def test_a_publish_reads_only_the_contents_it_can_have_changed(self):
        project = self.home.project("preaward", "settings.local.json", "base@tj-agents", "dotnet@tj-agents")
        self.home.installed(("dotnet@tj-agents", "local", project, OLD_HEAD))
        cases = (({"tj-agents/dotnet"}, True, ["dotnet"]),
                 ({"tj-agents/marketplace"}, True, ["core", "dotnet"]),
                 (None, True, ["core", "dotnet"]),
                 ({"tj-agents/dotnet"}, False, ["core", "dotnet"]))
        for repositories, seeded, expected in cases:
            with self.subTest(repositories=repositories, seeded=seeded):
                (self.home.root / "state/plugin-heads.json").unlink(missing_ok=True)
                if seeded:
                    self.record({"tj-agents/core@main": OLD_HEAD})
                queries = {}

                def query(arguments):
                    text = arguments[-1]
                    repo = re.search(r'name: "([^"]+)"', text)[1]
                    queries[repo] = text
                    return {"data": {"r": {"h": {"oid": {"core": CORE_HEAD, "dotnet": DOTNET_HEAD}[repo]},
                                           "t0": {"oid": "4" * 40}, "t1": {"oid": "5" * 40}}}}
                self.assertEqual(set(), REFRESH.refresh_heads(repositories, revision=self.fail, query=query))
                self.assertEqual(expected, sorted(queries))
                self.assertIn(f'"{OLD_HEAD}:plugins/dotnet"', queries["dotnet"])
                contents = REFRESH.plugin_autoupdate.read_contents()
                self.assertEqual(DOTNET_HEAD, contents["heads"]["tj-agents/dotnet@main"])
                self.assertIn(f"tj-agents/dotnet@{DOTNET_HEAD}:plugins/dotnet", contents["trees"])

    def test_one_unreadable_publisher_keeps_the_others_contents(self):
        def query(arguments):
            if 'name: "core"' in arguments[-1]:
                raise RuntimeError("Could not resolve to a Repository")
            return {"data": {"r": {"h": {"oid": DOTNET_HEAD}, "t0": {"oid": "4" * 40}}}}
        with mock.patch("sys.stderr"):
            self.assertEqual(set(), REFRESH.refresh_heads(None, revision=lambda url, ref: CORE_HEAD, query=query))
        contents = REFRESH.plugin_autoupdate.read_contents()
        self.assertEqual({"tj-agents/core@main": CORE_HEAD, "tj-agents/dotnet@main": DOTNET_HEAD}, contents["heads"])
        self.assertIn(f"tj-agents/dotnet@{DOTNET_HEAD}:plugins/dotnet", contents["trees"])

    def test_a_publish_installs_a_missing_declared_scope_and_retries_an_unreadable_named_head(self):
        project = self.home.project("preaward", "settings.local.json", "base@tj-agents", "dotnet@tj-agents")
        fake = FakeClaude(self.home.installed(("base@tj-agents", "local", project, CORE_HEAD)))

        def install(command, cwd):
            if command[2] == "install":
                fake.installations.append({"id": command[3], "scope": command[5], "projectPath": str(cwd),
                                           "enabled": True, "version": "v2"})
            return fake(command, cwd)
        with mock.patch.object(REFRESH, "run", side_effect=install), mock.patch("sys.stderr"):
            self.assertFalse(REFRESH.refresh_repositories(project, {"tj-agents/dotnet"}, revision=lambda url, ref: None,
                                                          query=lambda arguments: {"data": {}}))
        installs = [command[3] for command, _ in fake.commands if command[2] == "install"]
        self.assertEqual(["dotnet@tj-agents"], installs)
        self.assertNotIn("update", [command[2] for command, _ in fake.commands])

    def test_a_full_compare_tolerates_a_publisher_it_cannot_read(self):
        project = self.home.project("preaward", "settings.local.json", "base@tj-agents")
        self.home.installed(("base@tj-agents", "local", project, CORE_HEAD))
        query = lambda arguments: {"data": {"r": {"h": {"oid": CORE_HEAD}}}} if 'name: "core"' in arguments[-1] else {}
        with mock.patch.object(REFRESH, "run", side_effect=FakeClaude([])), mock.patch("sys.stderr"):
            self.assertTrue(REFRESH.refresh_repositories(project, None, revision=lambda url, ref: None, query=query))

    def test_a_marketplace_publish_updates_its_catalog_before_reading_it(self):
        project = self.home.project("preaward", "settings.local.json", "base@tj-agents")
        fake = FakeClaude(self.home.installed(("base@tj-agents", "local", project, CORE_HEAD)))
        query = lambda arguments: {"data": {"r": {"h": {"oid": CORE_HEAD if 'name: "core"' in arguments[-1] else DOTNET_HEAD}}}}
        with mock.patch.object(REFRESH, "run", side_effect=fake):
            self.assertTrue(REFRESH.refresh_repositories(project, {"tj-agents/marketplace"}, query=query))
        self.assertEqual([["claude", "plugin", "marketplace", "update", "tj-agents"]],
                         [command for command, _ in fake.commands])

    def test_no_launch_retries_an_update_at_the_same_head(self):
        project = self.home.project("preaward", "settings.local.json", "base@tj-agents")
        installations = self.home.installed(("base@tj-agents", "local", project, OLD_HEAD))
        self.record({"tj-agents/core@main": CORE_HEAD}, {f"tj-agents/core@{OLD_HEAD}:plugins/base": "1" * 40,
                                                       f"tj-agents/core@{CORE_HEAD}:plugins/base": "2" * 40})
        fake = FakeClaude(installations)
        with mock.patch.object(REFRESH, "run", side_effect=fake):
            REFRESH.reconcile_claude(project)
            first = len(fake.commands)
            self.assertEqual([], REFRESH.reconcile_claude(project))
            self.assertEqual(first, len(fake.commands))
            self.assertEqual([], REFRESH.reconcile_claude(project, event=True, repositories={"tj-agents/core"}))
            self.assertEqual([], REFRESH.reconcile_claude(project, event=True, repositories=None))
        self.assertEqual(first, len(fake.commands))

    def test_a_failing_update_is_tried_once_per_head(self):
        project = self.home.project("preaward", "settings.local.json", "base@tj-agents")
        installations = self.home.installed(("base@tj-agents", "local", project, OLD_HEAD))
        self.record({"tj-agents/core@main": CORE_HEAD}, {f"tj-agents/core@{OLD_HEAD}:plugins/base": "1" * 40,
                                                       f"tj-agents/core@{CORE_HEAD}:plugins/base": "2" * 40})
        fake = FakeClaude(installations)

        def failing(command, cwd):
            if command[2] == "update":
                raise REFRESH.RefreshError("network")
            return fake(command, cwd)
        with mock.patch.object(REFRESH, "run", side_effect=failing), self.assertRaises(REFRESH.RefreshError):
            REFRESH.reconcile_claude(project)
        before = len(fake.commands)
        with mock.patch.object(REFRESH, "run", side_effect=fake):
            self.assertEqual([], REFRESH.reconcile_claude(project))
            self.assertEqual(before, len(fake.commands))
            self.assertEqual([], REFRESH.reconcile_claude(project, event=True, repositories={"tj-agents/core"}))
            self.record({"tj-agents/core@main": DOTNET_HEAD}, {f"tj-agents/core@{DOTNET_HEAD}:plugins/base": "5" * 40})
            self.assertEqual(["base@tj-agents"], REFRESH.reconcile_claude(project, event=True,
                                                                         repositories={"tj-agents/core"}))

    def test_an_update_queued_after_a_failure_is_still_tried_by_the_next_launch(self):
        project = self.home.project("preaward", "settings.local.json", "base@tj-agents", "dotnet@tj-agents")
        installations = self.home.installed(("base@tj-agents", "local", project, OLD_HEAD),
                                            ("dotnet@tj-agents", "local", project, OLD_HEAD))
        self.record({"tj-agents/core@main": CORE_HEAD, "tj-agents/dotnet@main": DOTNET_HEAD},
                    {f"tj-agents/core@{OLD_HEAD}:plugins/base": "1" * 40,
                     f"tj-agents/core@{CORE_HEAD}:plugins/base": "2" * 40,
                     f"tj-agents/dotnet@{OLD_HEAD}:plugins/dotnet": "3" * 40,
                     f"tj-agents/dotnet@{DOTNET_HEAD}:plugins/dotnet": "4" * 40})
        fake = FakeClaude(installations)

        def failing(command, cwd):
            if command[2] == "update":
                raise REFRESH.RefreshError("network")
            return fake(command, cwd)
        with mock.patch.object(REFRESH, "run", side_effect=failing), self.assertRaises(REFRESH.RefreshError):
            REFRESH.reconcile_claude(project)
        with mock.patch.object(REFRESH, "run", side_effect=fake):
            self.assertEqual(["dotnet@tj-agents"], REFRESH.reconcile_claude(project))

    def test_a_non_git_subdirectory_does_not_shadow_its_parent_install(self):
        project = self.home.project("core", "settings.json", "base@tj-agents")
        nested = project / ".worktrees" / "Feature"
        self.home.installed(("base@tj-agents", "project", nested, OLD_HEAD), ("base@tj-agents", "project", project, OLD_HEAD))
        self.record({"tj-agents/core@main": CORE_HEAD}, {f"tj-agents/core@{OLD_HEAD}:plugins/base": "1" * 40,
                                                       f"tj-agents/core@{CORE_HEAD}:plugins/base": "2" * 40})
        fake = FakeClaude(REFRESH.installed_entries())
        with mock.patch.object(REFRESH, "run", side_effect=fake):
            self.assertEqual(['base@tj-agents'], REFRESH.reconcile_claude(project))
        self.assertEqual('v1', fake.installations[0]['version'])
        self.assertEqual('v2', fake.installations[1]['version'])
    def test_a_full_refresh_does_not_update_an_unrelated_subdirectory_install(self):
        project = self.home.project("core", "settings.json", "base@tj-agents")
        nested = project / ".worktrees" / "Feature"
        fake = FakeClaude(self.home.installed(("base@tj-agents", "project", nested, OLD_HEAD),
                                              ("base@tj-agents", "project", project, OLD_HEAD)))
        with mock.patch.object(REFRESH, "run", side_effect=fake):
            REFRESH.refresh("claude", "claude", project, {}, ["base@tj-agents"])
        self.assertEqual('v1', fake.installations[0]['version'])
        self.assertEqual('v2', fake.installations[1]['version'])

    def test_content_query_names_only_validated_revisions_and_paths(self):
        wanted = {"tj-agents/core@main": {"plugins/base", 'plugins/x") { oid } evil: object(expression: "y'}}
        captured = []
        heads, trees = REFRESH.plugin_autoupdate.github_contents(
            wanted, {"tj-agents/core@main": {OLD_HEAD, 'main") { oid } worse: object(expression: "z'}}, {},
            lambda arguments: captured.append(arguments[-1]) or {})
        self.assertEqual(({}, {}), (heads, trees))
        self.assertNotIn("evil", captured[0])
        self.assertNotIn("worse", captured[0])
        self.assertIn(f'"{OLD_HEAD}:plugins/base"', captured[0])


if __name__ == "__main__":
    unittest.main()
