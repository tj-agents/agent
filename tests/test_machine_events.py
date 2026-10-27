import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))
import machine_events as events
import install


def current(repository="tj-agents/core", revision="a", policy="policy", marketplace=True):
    return {"nameWithOwner": repository, "defaultBranchRef": {"name": "main", "target": {"oid": revision}},
            "policy": {"oid": policy} if policy else None,
            "marketplace": {"oid": "manifest"} if marketplace else None}


class HintTests(unittest.TestCase):
    def test_only_valid_repo_hints_are_accepted(self):
        for message in ({"v": 1, "repo": "other/repo"}, {"v": 1, "repo": "tj-agents/../x"},
                        {"v": True, "repo": "tj-agents/core"}, [], "garbage"):
            self.assertIsNone(events.parse_hint({"event": "message", "message": json.dumps(message)}))
        message = {"v": 1, "repo": "tj-agents/core", "policy": {"agents_may_merge": True}, "command": "evil"}
        self.assertEqual("tj-agents/core", events.parse_hint({"event": "message", "message": json.dumps(message)}))
        self.assertIsNone(events.parse_hint({"event": "keepalive"}))


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.refresh = mock.Mock(return_value=0)
        self.policy = mock.Mock()
        self.reconciler = events.Reconciler(refresh=self.refresh, policy=self.policy)

    def test_duplicate_hint_does_not_repeat_work_and_changed_policy_uses_only_repo(self):
        self.reconciler.repository(current())
        self.reconciler.repository(current())
        self.refresh.assert_called_once_with()
        self.policy.assert_called_once_with("tj-agents/core")
        self.reconciler.repository(current(revision="b", policy="new-policy", marketplace=False))
        self.assertEqual(2, self.policy.call_count)

    def test_a_revision_change_with_an_unchanged_catalog_never_refreshes(self):
        self.reconciler.repository(current())
        self.refresh.assert_called_once_with()
        self.reconciler.repository(current(revision="b"))
        self.refresh.assert_called_once_with()
        self.assertEqual("b", self.reconciler.state["tj-agents/core"]["revision"])

    def test_catalog_comparison_survives_a_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            self.reconciler.repository(current())
            self.refresh.assert_called_once_with()
            events.plugins.write_json(state_file, self.reconciler.state)
            reloaded = events.Reconciler(events.plugins.read_json(state_file), refresh=self.refresh, policy=self.policy)
            reloaded.repository(current(revision="b"))
        self.refresh.assert_called_once_with()

    def test_a_changed_catalog_oid_refreshes_again(self):
        self.reconciler.repository(current())
        self.refresh.assert_called_once_with()
        changed = current(revision="b")
        changed["marketplace"] = {"oid": "manifest-2"}
        self.reconciler.repository(changed)
        self.assertEqual(2, self.refresh.call_count)

    def test_standalone_plugin_changes_refresh_without_a_marketplace_manifest(self):
        state = current(policy=None, marketplace=False)
        state["plugin"] = {"oid": "plugin-manifest"}
        self.reconciler.repository(state)
        self.refresh.assert_called_once_with()
        self.policy.assert_not_called()

    def test_catch_up_refreshes_once_for_many_marketplaces(self):
        self.reconciler.catch_up([current(f"tj-agents/repo{i}") for i in range(50)])
        self.refresh.assert_called_once_with()
        self.assertEqual(50, self.policy.call_count)

    def test_repository_and_catch_up_reconciliation_never_launch_worktree_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.dict(os.environ),
                mock.patch.object(events, "RotatingFileHandler", return_value=logging.NullHandler()),
                mock.patch.object(events, "require_tommy"),
                mock.patch.object(events, "repository_state", return_value=current()),
                mock.patch.object(events, "catch_up_states", return_value=[current(), current("employer/project")]),
                mock.patch.object(events.Reconciler, "__init__", autospec=True, side_effect=self.bind),
                mock.patch.object(events.plugins, "sweep_worktrees") as cleanup,
            ):
                self.assertEqual(0, events.main(["--repo", "tj-agents/core", "--state-directory", directory]))
                self.assertEqual(0, events.main(["--state-directory", directory]))
        cleanup.assert_not_called()
        self.policy.assert_called_once_with("tj-agents/core")
        self.assertEqual(2, self.refresh.call_count)

    def bind(self, reconciler, state=None, refresh=None, policy=None):
        reconciler.state = state if state is not None else {}
        reconciler.refresh = self.refresh
        reconciler.policy = self.policy

    def test_plugin_refresh_keeps_the_generic_updater_cleanup_disabled(self):
        with mock.patch.object(events.plugins, "main", return_value=0) as update:
            self.assertEqual(0, events.refresh_plugins())
        self.assertIn("--no-sweep", update.call_args.args[0])

    def test_catch_up_upgrades_codex_marketplaces_while_a_codex_session_runs(self):
        def declared(executable, config, recorded, previous, repositories):
            target = {"tj-agents": {"source": "https://github.com/tj-agents/marketplace.git", "ref": None,
                                    "project": Path.home(), "plugins": ["base@tj-agents"]}}
            return {"marketplaces": events.plugins.refresh_codex(
                executable, target, recorded, revision=lambda *_: "b" * 40, run=lambda *_, **__: True,
                busy=lambda: True, packages=lambda name: [{"snapshot_first": False}], preserve=lambda *_: None)}

        with tempfile.TemporaryDirectory() as state,                 mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": state}),                 mock.patch.object(events.plugins.shutil, "which", return_value="codex"),                 mock.patch.object(events.plugins, "load_codex_config", return_value={}),                 mock.patch.object(events.plugins, "refresh_declared", side_effect=declared):
            self.assertEqual([], events.Reconciler(policy=self.policy).catch_up([]))

    def test_absent_policy_never_gets_a_default_gate(self):
        self.reconciler.repository(current(policy=None, marketplace=False))
        self.policy.assert_not_called()
        self.refresh.assert_not_called()

    def test_catch_up_isolates_one_policy_failure_and_continues(self):
        self.policy.side_effect = [RuntimeError("private repo"), None]
        with self.assertLogs(events.LOG, level="ERROR"):
            failures = self.reconciler.catch_up([current("tj-agents/private"), current("tj-agents/other")])
        self.assertEqual(["tj-agents/private"], failures)
        self.assertIn("tj-agents/other", self.reconciler.state)
        self.assertNotIn("tj-agents/private", self.reconciler.state)

    def test_failed_action_is_not_acknowledged(self):
        self.policy.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            self.reconciler.repository(current())
        self.assertEqual({}, self.reconciler.state)


class IdentityTests(unittest.TestCase):
    def test_saved_tommy_login_is_required(self):
        with self.assertRaises(RuntimeError):
            events.require_tommy(lambda _: {"login": "tomjseery-bot[bot]"})

    def test_tokens_and_agent_git_overrides_never_reach_policy_subprocess(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0))
        environment = {"TJ_AGENT": "1", "GH_TOKEN": "bot", "GITHUB_TOKEN": "bot",
                       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_VALUE_0": "helper", "GH_HOST": "other.example", "KEEP_ME": "yes"}
        with mock.patch.dict(os.environ, environment), mock.patch.object(events, "policy_script", return_value=Path("/installed/merge_policy.py")):
            events.reconcile_policy("tj-agents/core", run=run)
        saved = run.call_args.kwargs["env"]
        self.assertEqual("yes", saved["KEEP_ME"])
        self.assertEqual("github.com", saved["GH_HOST"])
        self.assertFalse(any(key.startswith("GIT_CONFIG_") or key in {"TJ_AGENT", "GH_TOKEN", "GITHUB_TOKEN"} for key in saved))
        self.assertEqual(["reconcile", "--repo", "tj-agents/core"], run.call_args.args[0][-3:])
        self.assertEqual(events.plugins.NO_WINDOW, run.call_args.kwargs["creationflags"])

    def test_listener_lock_has_no_age_expiry_and_releases_with_handle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            first = events.plugins.acquire_lock(path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(events.plugins.acquire_lock(path))
            finally:
                first.close()
            again = events.plugins.acquire_lock(path)
            self.assertIsNotNone(again)
            again.close()


class InstallTests(unittest.TestCase):
    def test_shared_topic_is_reused_without_writing_github_on_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            api = mock.Mock(return_value={"variables": [{"name": "MACHINE_EVENTS_TOPIC", "value": "a" * 48}]})
            self.assertEqual("a" * 48, install.configure_topic(Path(directory), api=api))
            self.assertEqual(1, api.call_count)
            api.reset_mock()
            self.assertEqual("a" * 48, install.configure_topic(Path(directory), api=api))
            self.assertEqual(1, api.call_count)

    def test_publisher_configuration_keeps_the_topic_masked_and_skips_current_settings(self):
        rows = [current("tj-agents/current"), current("tj-agents/new")]
        api = mock.Mock(side_effect=[{"variables": [{"name": "MACHINE_EVENTS_TOPIC", "value": "a" * 48}]},
                                    {"secrets": [{"name": "MACHINE_EVENTS_TOPIC"}]},
                                    {"variables": []}, {"secrets": []}, None])
        secret = mock.Mock()
        self.assertEqual(1, install.configure_publishers("a" * 48, rows, api=api, secret=secret))
        secret.assert_called_once_with("tj-agents/new", "a" * 48)
        self.assertEqual("POST", api.call_args.args[0][1])
        self.assertEqual("repos/tj-agents/new/actions/variables", api.call_args.args[0][2])

    def test_existing_variable_is_migrated_to_a_topic_secret(self):
        api = mock.Mock(side_effect=[{"variables": [{"name": "MACHINE_EVENTS_TOPIC", "value": "a" * 48}]},
                                    {"secrets": []}])
        secret = mock.Mock()
        self.assertEqual(1, install.configure_publishers("a" * 48, [current()], api=api, secret=secret))
        secret.assert_called_once_with("tj-agents/core", "a" * 48)
        self.assertEqual(2, api.call_count)

    def test_rotated_canonical_variable_refreshes_the_masked_secret(self):
        api = mock.Mock(side_effect=[{"variables": [{"name": "MACHINE_EVENTS_TOPIC", "value": "a" * 48,
                                                     "updated_at": "2026-10-20T12:00:00Z"}]},
                                    {"secrets": [{"name": "MACHINE_EVENTS_TOPIC", "updated_at": "2026-10-19T12:00:00Z"}]}])
        secret = mock.Mock()
        self.assertEqual(1, install.configure_publishers("a" * 48, [current()], api=api, secret=secret))
        secret.assert_called_once()

    def test_topic_secret_goes_through_stdin_and_not_command_arguments(self):
        with mock.patch.object(events, "github_cli") as cli:
            install.set_topic_secret("tj-agents/core", "a" * 48)
        self.assertEqual("a" * 48, cli.call_args.kwargs["input"])
        self.assertNotIn("a" * 48, cli.call_args.args[0])

    def test_installation_copies_the_complete_receipted_agent_runtime(self):
        with tempfile.TemporaryDirectory(prefix="runtime ü ") as directory:
            script = install.install_runtime(Path(directory), install.ROOT)
            self.assertEqual(Path(directory) / '.agents/agent/runtime/startup_binding.py', script)
            installed_receipt = json.loads((Path(directory) / "source.json").read_text(encoding="utf-8"))
            receipt = install.runtime_receipt(install.ROOT)
            self.assertEqual(receipt, install.runtime_delivery.validate_receipt(installed_receipt, install.ROOT))
            for name in receipt["files"]:
                self.assertEqual((install.ROOT / name).read_bytes(), (Path(directory) / name).read_bytes())
            result = subprocess.run([sys.executable, "-I", "-B", str(script), "--help"], capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)

    def test_the_listener_service_and_task_are_retired_and_never_scheduled(self):
        for name in ("schedule_windows", "schedule_systemd", "windows_task_xml"):
            self.assertFalse(hasattr(install, name))


if __name__ == "__main__":
    unittest.main()
