import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))
from codex import hook_trust as TRUST


class HookTrustTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cwd = Path(self.temporary.name).resolve()
        self.source = self.cwd / "source"
        self.cache = self.cwd / "cache"
        for root in (self.source, self.cache):
            (root / ".codex-plugin").mkdir(parents=True)
            (root / ".codex-plugin/plugin.json").write_text('{"name":"base"}')
            (root / "hooks").mkdir()
            (root / "hooks/codex.json").write_text('{"hooks":{}}')
            (root / "hooks/context.py").write_text('print("context")\n')
        self.plugin = {
            "pluginId": "base@tj-agents", "installed": True, "enabled": True,
            "marketplaceSource": {"sourceType": "git", "source": "https://github.com/tj-agents/core.git"},
            "source": {"source": "local", "path": str(self.source)},
        }
        self.hook = {
            "pluginId": "base@tj-agents", "source": "plugin", "isManaged": False,
            "enabled": True, "trustStatus": "modified",
            "sourcePath": str(self.cache / "hooks/codex.json"),
            "key": "base@tj-agents:hooks/codex.json:session_start:0:0",
            "currentHash": "sha256:" + "a" * 64,
            "eventName": "sessionStart", "matcher": None, "handlerType": "command", "command": "python startup.py",
        }
        self.archive = patch.object(TRUST, "committed_files", return_value=TRUST.package_files(self.source))
        self.archive.start()
        self.addCleanup(self.archive.stop)

    @patch.object(TRUST, "AppServer")
    def test_live_reload_rejects_unloaded_or_wrong_checkout_without_writing(self, app):
        for loaded, directory in (([], self.cwd), (["current"], self.cwd / "other")):
            with self.subTest(loaded=loaded, directory=directory):
                server = app.return_value.__enter__.return_value
                server.request.reset_mock()
                server.request.side_effect = [
                    {"data": loaded}, {"thread": {"id": "current", "cwd": str(directory)}},
                ]
                with self.assertRaises(RuntimeError):
                    TRUST.reload_running("native", self.cwd, {"base@tj-agents": []}, session_id="current")
                self.assertNotIn("config/batchWrite", [call.args[0] for call in server.request.call_args_list])

    @patch.object(TRUST, "AppServer")
    def test_live_reload_uses_native_no_edit_reload_then_waits_for_complete_definitions(self, app):
        hook = dict(self.hook, trustStatus="trusted")
        listing = {"data": [{"cwd": str(self.cwd), "errors": [], "hooks": [hook]}]}
        server = app.return_value.__enter__.return_value
        server.request.side_effect = [
            {"data": ["current"]}, {"thread": {"id": "current", "cwd": str(self.cwd)}},
            {"status": "ok"}, {"data": [{"cwd": str(self.cwd), "errors": [], "hooks": []}]}, listing,
        ]
        required = {"base@tj-agents": [TRUST.signature(hook)]}
        with patch.object(TRUST.time, "sleep"):
            TRUST.reload_running("native", self.cwd, required, session_id="current")
        app.assert_called_once_with("native", self.cwd, timeout=45, existing=True)
        server.request.assert_any_call("config/batchWrite", {"edits": [], "reloadUserConfig": True})
        self.assertEqual(2, sum(call.args[0] == "hooks/list" for call in server.request.call_args_list))

    @patch.object(TRUST, "AppServer")
    def test_live_reload_does_not_accept_modified_hooks(self, app):
        server = app.return_value.__enter__.return_value
        server.request.side_effect = [
            {"data": ["current"]}, {"thread": {"id": "current", "cwd": str(self.cwd)}},
            {"status": "ok"}, {"data": [{"cwd": str(self.cwd), "errors": [], "hooks": [self.hook]}]},
        ]
        with self.assertRaisesRegex(RuntimeError, "modified"):
            TRUST.reload_running("native", self.cwd, {"base@tj-agents": [TRUST.signature(self.hook)]},
                                 session_id="current", timeout=0)

    @patch.object(TRUST, "AppServer")
    def test_terminal_refresh_without_native_thread_does_not_start_daemon(self, app):
        with patch.dict(TRUST.os.environ, {}, clear=True):
            TRUST.reload_running("native", self.cwd, {"base@tj-agents": []})
        app.assert_not_called()

    def updates(self, *hooks):
        return TRUST.trust_updates(
            {"installed": [self.plugin]},
            [{"cwd": str(self.cwd), "errors": [], "hooks": list(hooks)}], self.cwd,
        )

    def git_result(self, arguments, cwd):
        if "--show-toplevel" in arguments:
            return str(self.cwd)
        if "get-url" in arguments:
            return "git@github.com:tj-agents/core.git"
        if "status" in arguments:
            return ""
        if "HEAD" in arguments and "rev-parse" in arguments:
            return "b" * 40
        if "ls-remote" in arguments:
            return "b" * 40 + "\trefs/heads/main"
        self.fail(f"Unexpected Git command: {arguments}")

    def test_accepts_exact_github_owner_in_supported_url_forms(self):
        for value in (
            "tj-agents/core", "https://github.com/tj-agents/core.git",
            "git@github.com:tj-agents/core.git", "ssh://git@github.com/TJ-AGENTS/core.git",
        ):
            with self.subTest(value=value):
                self.assertEqual(("tj-agents", "core"), TRUST.github_repository(value))

    def test_rejects_lookalike_hosts_credentials_and_owner_prefixes(self):
        for value in (
            "https://github.com.evil/tj-agents/core.git",
            "https://github.com@evil/tj-agents/core.git",
            "https://evil/github.com/tj-agents/core.git",
            "https://github.com/tj-agents/core.git?source=evil",
            "https://github.com/tj-agents/core.git/other",
        ):
            with self.subTest(value=value):
                self.assertIsNone(TRUST.github_repository(value))
        self.assertNotEqual(TRUST.TRUSTED_OWNER, TRUST.github_repository("tj-agents-evil/core")[0])

    @patch.object(TRUST, "run")
    def test_rejects_unpublished_local_commit(self, runner):
        runner.side_effect = lambda args, cwd: "c" * 40 + "\trefs/heads/main" if "ls-remote" in args else self.git_result(args, cwd)
        with self.assertRaisesRegex(RuntimeError, "not published"):
            self.updates(self.hook)

    @patch.object(TRUST, "run")
    def test_only_tj_agents_plugin_hashes_are_selected(self, runner):
        runner.side_effect = self.git_result
        outsider = dict(self.hook, pluginId="other@tj-agents", key="other@tj-agents:hook")
        project = dict(self.hook, source="project", pluginId=None, key="project:hook")
        user = dict(self.hook, source="user", pluginId=None, key="user:hook")
        disabled = dict(self.hook, enabled=False, key="base@tj-agents:disabled")
        trusted = dict(self.hook, trustStatus="trusted", key="base@tj-agents:trusted")
        self.assertEqual(
            {self.hook["key"]: {"trusted_hash": self.hook["currentHash"]}},
            self.updates(self.hook, outsider, project, user, disabled, trusted),
        )

    def test_marketplace_name_does_not_grant_trust(self):
        self.plugin["marketplaceSource"]["source"] = "https://github.com/other/core.git"
        with patch.object(TRUST, "run") as runner:
            self.assertEqual({}, self.updates(self.hook))
            runner.assert_not_called()

    def test_local_marketplace_does_not_grant_trust(self):
        self.plugin["marketplaceSource"]["sourceType"] = "local"
        self.assertEqual({}, self.updates(self.hook))

    @patch.object(TRUST, "run")
    def test_git_subdir_uses_published_objects_and_preserves_native_composite_hash(self, runner):
        self.plugin["source"] = {"source": "git-subdir", "url": "https://github.com/tj-agents/core.git",
                                 "path": "plugins/base", "ref": "main"}
        self.hook["currentHash"] += "/sha256:" + "b" * 64
        runner.side_effect = lambda args, cwd: "c" * 40 if "rev-parse" in args else ""
        self.assertEqual({self.hook["key"]: {"trusted_hash": self.hook["currentHash"]}}, self.updates(self.hook))
        TRUST.committed_files.assert_called_once()
        self.assertEqual(("c" * 40, "plugins/base", self.cwd), TRUST.committed_files.call_args.args[1:])
        fetch = next(call.args[0] for call in runner.call_args_list if "fetch" in call.args[0])
        self.assertEqual(["https://github.com/tj-agents/core.git", "main"], fetch[-2:])
        (self.cache / "hooks/context.py").write_text('print("changed")\n')
        with self.assertRaisesRegex(RuntimeError, "differs from its Git source"):
            self.updates(self.hook)

    @patch.object(TRUST, "run")
    def test_git_subdir_rejects_untrusted_publishers_paths_and_revisions(self, runner):
        source = {"source": "git-subdir", "url": "https://github.com/tj-agents/core.git",
                  "path": "plugins/base", "ref": "main"}
        for changes in ({"url": "https://github.com/other/core.git"}, {"path": "../base"},
                        {"path": "/base"}, {"path": "C:\\base"}, {"ref": "--upload-pack=other"}):
            self.plugin["source"] = dict(source, **changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, "Invalid published"):
                self.updates(self.hook)
        runner.assert_not_called()

    @patch.object(TRUST, "run")
    def test_rejects_malformed_native_hashes(self, runner):
        runner.side_effect = self.git_result
        for value in ("sha256:" + "a" * 63, "sha256:" + "a" * 64 + "/other", "/".join(["sha256:" + "a" * 64] * 3)):
            self.hook["currentHash"] = value
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "Invalid native hook"):
                self.updates(self.hook)

    @patch.object(TRUST, "run")
    def test_rejects_changed_source(self, runner):
        runner.side_effect = lambda args, cwd: " M source/hooks/context.py" if "status" in args else self.git_result(args, cwd)
        with self.assertRaisesRegex(RuntimeError, "local changes"):
            self.updates(self.hook)

    @patch.object(TRUST, "run")
    def test_rejects_changed_installed_dependency(self, runner):
        runner.side_effect = self.git_result
        (self.cache / "hooks/context.py").write_text('print("changed")\n')
        with self.assertRaisesRegex(RuntimeError, "differs from its Git source"):
            self.updates(self.hook)

    @patch.object(TRUST, "run")
    def test_rejects_unexpected_installed_executable_file(self, runner):
        runner.side_effect = self.git_result
        (self.cache / "hooks/__pycache__").mkdir()
        (self.cache / "hooks/__pycache__/context.cpython-313.pyc").write_bytes(b"\x00")
        with self.assertRaisesRegex(RuntimeError, "differs from its Git source"):
            self.updates(self.hook)

    @patch.object(TRUST, "run")
    def test_rejects_source_origin_mismatch(self, runner):
        runner.side_effect = lambda args, cwd: "https://github.com/other/core.git" if "get-url" in args else self.git_result(args, cwd)
        with self.assertRaisesRegex(RuntimeError, "origin differs"):
            self.updates(self.hook)

    def test_package_is_read_from_git_objects_instead_of_ignored_working_files(self):
        self.archive.stop()
        def git(*arguments):
            return subprocess.run(
                ["git", "-C", str(self.cwd), *arguments], check=True,
                capture_output=True, text=True,
            ).stdout.strip()
        git("init")
        git("add", "source")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "fixture")
        revision = git("rev-parse", "HEAD")
        git("update-index", "--assume-unchanged", "source/hooks/context.py")
        (self.source / "hooks/context.py").write_text('print("changed")\n')
        (self.cwd / ".git/info/exclude").write_text("source/hooks/injected.py\n")
        (self.source / "hooks/injected.py").write_text('print("injected")\n')
        self.assertEqual("", git("status", "--porcelain", "--", "source"))
        files = TRUST.committed_files(self.cwd, revision, "source", self.cwd)
        self.assertEqual(b'print("context")\n', files["hooks/context.py"])
        self.assertNotIn("hooks/injected.py", files)
        root_files = TRUST.committed_files(self.cwd, revision, ".", self.cwd)
        self.assertEqual(b'print("context")\n', root_files["source/hooks/context.py"])

    @patch.object(TRUST, "AppServer")
    @patch.object(TRUST, "run")
    def test_native_batch_write_only_upserts_selected_hashes(self, runner, server_class):
        runner.return_value = json.dumps({"installed": []})
        server = server_class.return_value.__enter__.return_value
        trusted = dict(self.hook, trustStatus="trusted")
        server.request.side_effect = lambda method, params: (
            {"config": {}} if method == "config/read" else
            {"data": [{"cwd": str(self.cwd), "hooks": [trusted if server.request.call_count > 3 else self.hook]}]}
            if method == "hooks/list" else {}
        )
        selected = {self.hook["key"]: {"trusted_hash": self.hook["currentHash"]}}
        with patch.object(TRUST, "trust_updates", return_value=selected):
            self.assertEqual(selected, TRUST.apply_trust("codex", self.cwd))
        server.request.assert_any_call("config/batchWrite", {
            "edits": [{"keyPath": "hooks.state", "value": selected, "mergeStrategy": "upsert"}],
            "reloadUserConfig": True,
        })

    @patch.object(TRUST, "AppServer")
    @patch.object(TRUST, "run")
    def test_manual_trust_never_writes_and_still_requires_ready_hooks(self, runner, server_class):
        settings = self.cwd / ".agents/settings.json"
        settings.parent.mkdir()
        (self.cwd / ".agents/plugins.json").write_text("{}", encoding="utf-8")
        settings.write_text(json.dumps({"configuration": {"core": {"trust_hooks": False}}}), encoding="utf-8")
        nested = self.cwd / "nested"
        nested.mkdir()
        server = server_class.return_value.__enter__.return_value
        for state in ("modified", "trusted"):
            with self.subTest(state=state):
                server.request.reset_mock()
                hook = dict(self.hook, trustStatus=state)
                server.request.side_effect = lambda method, params: (
                    {"config": {}} if method == "config/read" else
                    {"data": [{"cwd": str(nested), "hooks": [hook]}]}
                )
                required = {hook["pluginId"]: [TRUST.signature(hook)]}
                if state == "modified":
                    with self.assertRaisesRegex(RuntimeError, "not ready"):
                        TRUST.apply_trust("codex", nested, required=required)
                else:
                    self.assertEqual({}, TRUST.apply_trust("codex", nested, required=required))
                self.assertEqual(["config/read", "hooks/list"], [call.args[0] for call in server.request.call_args_list])
        runner.assert_not_called()

    @patch.object(TRUST, "AppServer")
    @patch.object(TRUST, "run")
    def test_preview_does_not_write_config(self, runner, server_class):
        runner.return_value = json.dumps({"installed": []})
        server = server_class.return_value.__enter__.return_value
        trusted = dict(self.hook, trustStatus="trusted")
        server.request.side_effect = lambda method, params: (
            {"config": {}} if method == "config/read" else
            {"data": [{"cwd": str(self.cwd), "hooks": [trusted if server.request.call_count > 3 else self.hook]}]}
            if method == "hooks/list" else {}
        )
        with patch.object(TRUST, "trust_updates", return_value={"hook": {"trusted_hash": "hash"}}):
            TRUST.apply_trust("codex", self.cwd, preview=True)
        self.assertEqual(["config/read", "hooks/list"], [call.args[0] for call in server.request.call_args_list])

    def test_readiness_rejects_missing_inventory_loading_errors_and_disabled_required_hooks(self):
        for listing in (
            {"data": []},
            {"data": [{"cwd": str(self.cwd), "hooks": []}]},
            {"data": [{"cwd": str(self.cwd), "errors": ["unreadable hook"], "hooks": []}]},
            {"data": [{"cwd": str(self.cwd), "hooks": [dict(self.hook, enabled=False)]}]},
            {"data": [{"cwd": str(self.cwd), "hooks": [self.hook]}]},
        ):
            with self.subTest(listing=listing), self.assertRaises(RuntimeError):
                TRUST.require_ready(listing, self.cwd, required={"base@tj-agents": [TRUST.signature(self.hook)]})

    def test_one_present_hook_does_not_mask_a_missing_required_definition(self):
        trusted = dict(self.hook, trustStatus="trusted")
        listing = {"data": [{"cwd": str(self.cwd), "hooks": [trusted]}]}
        definitions = [TRUST.signature(trusted), ("preToolUse", "Write", "command", "python gate.py")]
        with self.assertRaisesRegex(RuntimeError, "definitions missing"):
            TRUST.require_ready(listing, self.cwd, required={"base@tj-agents": definitions})
        with self.assertRaisesRegex(RuntimeError, "definitions missing"):
            TRUST.require_ready(listing, self.cwd, required={"base@tj-agents": [TRUST.signature(trusted)] * 2})

    @patch.object(TRUST, "AppServer")
    def test_unrelated_untrusted_hooks_are_neither_approved_nor_required(self, server_class):
        trusted = dict(self.hook, trustStatus="trusted")
        unrelated = dict(self.hook, pluginId="other@publisher", key="other@publisher:hook")
        server = server_class.return_value.__enter__.return_value
        server.request.side_effect = lambda method, params: (
            {"config": {}} if method == "config/read" else
            {"data": [{"cwd": str(self.cwd), "hooks": [trusted, unrelated]}]}
        )
        with patch.object(TRUST, "trust_updates") as updates:
            self.assertEqual({}, TRUST.apply_trust("codex", self.cwd,
                             required={"base@tj-agents": [TRUST.signature(trusted)]}))
        updates.assert_not_called()

    def test_required_definitions_resolve_platform_commands_and_keep_duplicate_slots(self):
        (self.cache / ".codex-plugin/plugin.json").write_text('{"hooks":"hooks/codex.json"}')
        handler = {"type": "command", "command": "python ${PLUGIN_ROOT}/start.py",
                   "commandWindows": "python ${PLUGIN_ROOT}/win.py"}
        (self.cache / "hooks/codex.json").write_text(json.dumps({"hooks": {
            "SessionStart": [{"hooks": [handler, handler]}],
        }}))
        import os
        name = "win.py" if os.name == "nt" else "start.py"
        self.assertEqual([("sessionStart", None, "command", f"python {self.cache}/{name}")] * 2,
                         TRUST.required_definitions(self.cache, "base@tj-agents"))

    def test_readiness_rejects_hash_changes_after_the_verified_write(self):
        listing = {"data": [{"cwd": str(self.cwd), "hooks": [dict(self.hook, trustStatus="trusted")]}]}
        with self.assertRaisesRegex(RuntimeError, "contents changed"):
            TRUST.require_ready(listing, self.cwd, expected={self.hook["key"]: {"trusted_hash": "new"}})

    @patch.object(TRUST, "AppServer")
    @patch.object(TRUST, "run")
    def test_a_successful_config_write_with_untrusted_readback_is_a_failure(self, runner, server_class):
        runner.return_value = json.dumps({"installed": []})
        server = server_class.return_value.__enter__.return_value
        server.request.side_effect = lambda method, params: (
            {"config": {}} if method == "config/read" else
            {"data": [{"cwd": str(self.cwd), "hooks": [self.hook]}]} if method == "hooks/list" else {}
        )
        with patch.object(TRUST, "trust_updates", return_value={
            self.hook["key"]: {"trusted_hash": self.hook["currentHash"]},
        }), self.assertRaisesRegex(RuntimeError, "not ready"):
            TRUST.apply_trust("codex", self.cwd)

    @patch.object(TRUST, "AppServer")
    def test_disabled_effective_hooks_are_not_silently_enabled(self, server_class):
        server = server_class.return_value.__enter__.return_value
        server.request.return_value = {"config": {"features": {"hooks": False}}}
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            TRUST.apply_trust("codex", self.cwd)
        self.assertEqual(["config/read"], [call.args[0] for call in server.request.call_args_list])

    @patch.object(TRUST, "AppServer")
    def test_uses_the_remaining_preflight_deadline_for_app_server_requests(self, server_class):
        server = server_class.return_value.__enter__.return_value
        server.request.side_effect = lambda method, params: (
            {"config": {}} if method == "config/read" else {"data": [{"cwd": str(self.cwd), "hooks": []}]}
        )
        self.assertEqual({}, TRUST.apply_trust("codex", self.cwd, timeout=7))
        self.assertEqual(server_class.call_args.kwargs["timeout"], 7)

if __name__ == "__main__":
    unittest.main()
