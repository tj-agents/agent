import json
import queue
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))
from codex import hook_trust as TRUST


def test_batch_replies_are_routed_by_id_and_preserve_duplicate_entries():
    server = TRUST.AppServer.__new__(TRUST.AppServer)
    server.sequence = 0
    server.timeout = 1
    server.messages = queue.Queue()
    server.send = mock.Mock()
    paths = [Path("a"), Path("b"), Path("a"), Path("c"), Path("d"), Path("e")]
    entries = [{"cwd": str(path), "hooks": [{"key": str(index)}], "errors": []}
               for index, path in enumerate(paths)]
    server.messages.put({"method": "notification"})
    server.messages.put({"id": 2, "method": "server/request", "params": {}})
    for request_id, data in ((3, entries[4:]), (1, entries[:2]), (2, entries[2:4])):
        server.messages.put({"id": request_id, "result": {"data": data}})

    assert TRUST.list_installed_hooks(server, paths) == {"data": entries}
    assert [call.args[0]["id"] for call in server.send.call_args_list] == [1, 2, 3]


@pytest.mark.parametrize(("response", "message"), [({"id": 2, "error": "bad"}, "failed"), (None, "timed out")])
def test_batch_error_or_timeout_fails(response, message):
    server = TRUST.AppServer.__new__(TRUST.AppServer)
    server.sequence = 0
    server.timeout = 0
    server.messages = queue.Queue()
    server.send = mock.Mock()
    if response is not None:
        server.messages.put(response)
    with pytest.raises(RuntimeError, match=message):
        server.requests("hooks/list", [{"cwds": ["a"]}, {"cwds": ["b"]}])


def test_batch_rejects_wrong_directory_even_when_entry_count_matches():
    server = mock.Mock()
    server.request.return_value = {"data": [{"cwd": "other", "hooks": [], "errors": []}]}
    with pytest.raises(RuntimeError, match="does not match"):
        TRUST.list_installed_hooks(server, [Path("expected")])


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
            {"status": "ok"}, listing,
            {"data": [{"cwd": str(self.cwd), "errors": [], "hooks": []}]}, listing,
        ]
        required = {"base@tj-agents": [TRUST.signature(hook)]}
        with patch.object(TRUST.time, "sleep"):
            TRUST.reload_running("native", self.cwd, required, session_id="current")
        app.assert_called_once_with("native", self.cwd, timeout=45, existing=True)
        server.request.assert_any_call("config/batchWrite", {"edits": [], "reloadUserConfig": True})
        self.assertEqual(3, sum(call.args[0] == "hooks/list" for call in server.request.call_args_list))
        server.request.assert_any_call("hooks/list", {"threadId": "current"})

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
            [{"cwd": str(self.cwd), "errors": [], "hooks": list(hooks)}], {self.cwd},
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
    def test_ignores_generated_installed_bytecode(self, runner):
        runner.side_effect = self.git_result
        (self.cache / "hooks/__pycache__").mkdir()
        (self.cache / "hooks/__pycache__/context.cpython-313.pyc").write_bytes(b"\x00")
        self.assertEqual(
            {self.hook["key"]: {"trusted_hash": self.hook["currentHash"]}},
            self.updates(self.hook),
        )

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


@dataclass(frozen=True, slots=True)
class HookTrustState:
    cwd: Path
    plugin: dict[str, Any]
    hook: dict[str, Any]

    def git_result(self, arguments: list[str], cwd: Path) -> str:
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
        raise AssertionError(f"Unexpected Git command: {arguments}")


@pytest.fixture
def hook_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HookTrustState:
    cwd = tmp_path.resolve()
    source, cache = cwd / "source", cwd / "cache"
    for root in (source, cache):
        (root / ".codex-plugin").mkdir(parents=True)
        (root / ".codex-plugin/plugin.json").write_text(
            '{"name":"base"}', encoding="utf-8"
        )
        (root / "hooks").mkdir()
        (root / "hooks/codex.json").write_text('{"hooks":{}}', encoding="utf-8")
        (root / "hooks/context.py").write_text('print("context")\n', encoding="utf-8")
    plugin = {
        "pluginId": "base@tj-agents",
        "installed": True,
        "enabled": True,
        "marketplaceSource": {
            "sourceType": "git",
            "source": "https://github.com/tj-agents/core.git",
        },
        "source": {"source": "local", "path": str(source)},
    }
    hook = {
        "pluginId": "base@tj-agents",
        "source": "plugin",
        "isManaged": False,
        "enabled": True,
        "trustStatus": "modified",
        "sourcePath": str(cache / "hooks/codex.json"),
        "key": "base@tj-agents:hooks/codex.json:session_start:0:0",
        "currentHash": "sha256:" + "a" * 64,
        "eventName": "sessionStart",
        "matcher": None,
        "handlerType": "command",
        "command": "python startup.py",
    }
    monkeypatch.setattr(
        TRUST, "committed_files", lambda *_: TRUST.package_files(source)
    )
    return HookTrustState(cwd, plugin, hook)


@pytest.mark.parametrize("stale", ["hash", "path", "removed", "missing"])
def test_reload_rejects_stale_thread_hooks_despite_fresh_disk_inventory(
    hook_state: HookTrustState, monkeypatch: pytest.MonkeyPatch, stale: str
) -> None:
    fresh = dict(hook_state.hook, trustStatus="trusted")
    if stale == "hash":
        loaded = [dict(fresh, currentHash="sha256:" + "b" * 64)]
        message = "contents changed"
    elif stale == "path":
        loaded = [dict(fresh, sourcePath=str(hook_state.cwd / "old-build/hooks.json"))]
        message = "different plugin build path"
    elif stale == "removed":
        loaded = [fresh, dict(fresh, key="removed-hook")]
        message = "removed plugin hook"
    else:
        loaded = []
        message = "missing"
    server = mock.MagicMock()
    server.__enter__.return_value = server

    def request(method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "thread/loaded/list":
            return {"data": ["current"]}
        if method == "thread/read":
            return {"thread": {"id": "current", "cwd": str(hook_state.cwd)}}
        if method == "config/batchWrite":
            return {"status": "ok"}
        assert method == "hooks/list"
        hooks = loaded if params == {"threadId": "current"} else [fresh]
        return {"data": [{"cwd": str(hook_state.cwd), "hooks": hooks, "errors": []}]}

    server.request.side_effect = request
    monkeypatch.setattr(TRUST, "AppServer", lambda *args, **kwargs: server)
    with pytest.raises(RuntimeError, match=message):
        TRUST.reload_running(
            "native", hook_state.cwd, {"base@tj-agents": [TRUST.signature(fresh)]},
            session_id="current", timeout=0,
        )


def test_reload_finds_thread_beyond_first_inventory_page(
    hook_state: HookTrustState, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = dict(hook_state.hook, trustStatus="trusted")
    listing = {"data": [{"cwd": str(hook_state.cwd), "hooks": [fresh], "errors": []}]}
    server = mock.MagicMock()
    server.__enter__.return_value = server
    server.request.side_effect = [
        {"data": ["other"], "nextCursor": "next"},
        {"data": ["current"]},
        {"thread": {"id": "current", "cwd": str(hook_state.cwd)}},
        {"status": "ok"}, listing, listing,
    ]
    monkeypatch.setattr(TRUST, "AppServer", lambda *args, **kwargs: server)
    TRUST.reload_running(
        "native", hook_state.cwd, {"base@tj-agents": [TRUST.signature(fresh)]},
        session_id="current",
    )
    server.request.assert_any_call("thread/loaded/list", {"cursor": "next"})
    server.request.assert_any_call("hooks/list", {"threadId": "current"})


def test_reload_rejects_repeated_inventory_cursor(
    hook_state: HookTrustState, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = mock.MagicMock()
    server.__enter__.return_value = server
    server.request.return_value = {"data": [], "nextCursor": "same"}
    monkeypatch.setattr(TRUST, "AppServer", lambda *args, **kwargs: server)
    with pytest.raises(RuntimeError, match="complete the loaded"):
        TRUST.reload_running(
            "native", hook_state.cwd, {"base@tj-agents": []}, session_id="current"
        )
    assert server.request.call_count == 2


def test_verify_loaded_hooks_rejects_plugin_removed_from_project(
    hook_state: HookTrustState,
) -> None:
    fresh = {"data": [{"cwd": str(hook_state.cwd), "hooks": [], "errors": []}]}
    stale = {
        "data": [{"cwd": str(hook_state.cwd), "errors": [],
                  "hooks": [dict(hook_state.hook, trustStatus="trusted")]}]
    }
    server = mock.Mock()
    server.request.side_effect = [fresh, stale]
    with pytest.raises(RuntimeError, match="removed plugin hook"):
        TRUST.verify_loaded_hooks(server, hook_state.cwd, "current", required={}, deadline=0)
    server.request.assert_called_with("hooks/list", {"threadId": "current"})


@pytest.mark.parametrize("loaded_hash", ["declaration", "composite", "wrong-content", "wrong-declaration"])
def test_loaded_native_declaration_hash_is_distinct_from_installed_content_approval(
    hook_state: HookTrustState, loaded_hash: str
) -> None:
    declaration = "sha256:" + "a" * 64
    content = "sha256:" + "b" * 64
    fresh = dict(hook_state.hook, currentHash=f"{declaration}/{content}", trustStatus="trusted")
    hashes = {
        "declaration": declaration,
        "composite": f"{declaration}/{content}",
        "wrong-content": f"{declaration}/sha256:" + "c" * 64,
        "wrong-declaration": "sha256:" + "d" * 64,
    }
    server = mock.Mock()
    server.request.side_effect = [
        {"data": [{"cwd": str(hook_state.cwd), "hooks": [fresh], "errors": []}]},
        {"data": [{"cwd": str(hook_state.cwd), "errors": [],
                   "hooks": [dict(fresh, currentHash=hashes[loaded_hash])]}]},
    ]
    if loaded_hash.startswith("wrong-"):
        with pytest.raises(RuntimeError, match="contents changed"):
            TRUST.verify_loaded_hooks(server, hook_state.cwd, "current", required={}, deadline=0)
    else:
        TRUST.verify_loaded_hooks(server, hook_state.cwd, "current", required={}, deadline=0)


@pytest.mark.parametrize("with_options", [False, True])
def test_apply_trust_scopes_native_inventory_with_and_without_launch_options(
    hook_state: HookTrustState, monkeypatch: pytest.MonkeyPatch, with_options: bool
) -> None:
    server = mock.MagicMock()
    server.__enter__.return_value = server
    server.request.side_effect = lambda method, _params: (
        {"config": {}} if method == "config/read" else
        {"data": [{"cwd": str(hook_state.cwd), "hooks": [hook_state.hook], "errors": []}]}
    )
    monkeypatch.setattr(TRUST, "AppServer", lambda *_args, **_kwargs: server)
    monkeypatch.setattr(TRUST, "trust_updates", lambda *_args: {})
    calls = []

    def scoped(arguments, cwd, *_args, **kwargs):
        if "--marketplace" not in arguments:
            raise RuntimeError("unfiltered native inventory is unavailable")
        calls.append(arguments)
        assert arguments[arguments.index("--marketplace") + 1] == "tj-agents"
        assert cwd == hook_state.cwd
        if with_options:
            assert kwargs["env"]["SCOPE_FIXTURE"] == "present"
            return subprocess.CompletedProcess(arguments, 0, '{"installed": []}', "")
        return '{"installed": []}'

    monkeypatch.setattr(TRUST, "run", scoped)
    monkeypatch.setattr(TRUST.process, "run", scoped)
    options = ("-c", "override") if with_options else ()
    environment = {"SCOPE_FIXTURE": "present"} if with_options else None
    assert TRUST.apply_trust("codex", hook_state.cwd, preview=True, launch_args=options, environment=environment) == {}
    assert calls == [["codex", *options, "plugin", "list", "--marketplace", "tj-agents", "--json"]]


@patch.object(TRUST, "run")
@patch.object(TRUST, "AppServer")
def test_installed_hooks_are_trusted_only_in_trusted_projects(
    app: mock.Mock, runner: mock.Mock, hook_state: HookTrustState
) -> None:
    codex_home = hook_state.cwd / "codex-home"
    build = codex_home / "plugins/cache/tj-agents/base/build-2"
    (build / ".codex-plugin").mkdir(parents=True)
    (build / ".codex-plugin/plugin.json").write_text(
        '{"name": "base", "hooks": "./hooks/codex.json"}'
    )
    (build / "hooks").mkdir()
    (build / "hooks/codex.json").write_text(
        '{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "python startup.py"}]}]}}'
    )
    staging = (
        codex_home / "plugins/cache/tj-agents/plugin-install-AbC123/agent/.codex-plugin"
    )
    staging.mkdir(parents=True)
    (staging / "plugin.json").write_text(
        '{"name": "agent", "hooks": "./hooks/codex.json"}'
    )
    project = hook_state.cwd / "project"
    (project / ".codex").mkdir(parents=True)
    (project / ".codex/config.toml").write_text("")
    state = hook_state.cwd / "state"
    third_party = dict(
        hook_state.hook,
        pluginId="other@elsewhere",
        key="other@elsewhere:hooks.json:stop:0:0",
        trustStatus="untrusted",
    )
    listing = {
        "data": [
            {"cwd": str(hook_state.cwd), "errors": [], "hooks": [hook_state.hook]},
            {
                "cwd": str(project),
                "errors": [],
                "hooks": [hook_state.hook, third_party],
            },
        ]
    }
    other = {
        "pluginId": "other@elsewhere",
        "installed": True,
        "enabled": True,
        "marketplaceSource": {
            "sourceType": "git",
            "source": "https://github.com/someone/plugins.git",
        },
    }
    inventory = json.dumps({"installed": [hook_state.plugin, other]})
    def scoped_inventory(arguments, cwd, *_):
        if "plugin" not in arguments:
            return hook_state.git_result(arguments, cwd)
        if arguments[arguments.index("list") + 1 : arguments.index("list") + 3] != ["--marketplace", "tj-agents"]:
            raise RuntimeError("unfiltered native inventory is unavailable")
        return json.dumps({"installed": []}) if cwd == hook_state.cwd else inventory

    runner.side_effect = scoped_inventory
    server = app.return_value.__enter__.return_value
    trusted = {
        "data": [
            dict(
                entry,
                hooks=[
                    dict(hook, trustStatus="trusted")
                    if hook is hook_state.hook
                    else hook
                    for hook in entry["hooks"]
                ],
            )
            for entry in listing["data"]
        ]
    }
    listings = iter([listing, trusted])
    server.request.side_effect = lambda method, params: (
        next(listings) if method == "hooks/list" else {}
    )
    with (
        patch.dict(TRUST.os.environ, {"CODEX_HOME": str(codex_home)}),
        patch.object(TRUST.Path, "home", return_value=hook_state.cwd),
        patch.object(
            TRUST.projects,
            "user_config",
            return_value={"projects": {str(project): {"trust_level": "trusted"}}},
        ),
        patch.object(
            TRUST.projects.tempfile,
            "gettempdir",
            return_value=str(hook_state.cwd / "elsewhere"),
        ),
    ):
        assert TRUST.pending(state) == ["trust base@tj-agents hooks"]
        updates = TRUST.trust_installed("codex", state, ("-c", "override"))
        assert TRUST.pending(state) == []
    assert {
        hook_state.hook["key"]: {"trusted_hash": hook_state.hook["currentHash"]}
    } == updates
    server.request.assert_any_call(
        "hooks/list", {"cwds": [str(hook_state.cwd), str(project.resolve())]}
    )
    server.request.assert_any_call(
        "config/batchWrite",
        {
            "edits": [
                {"keyPath": "hooks.state", "value": updates, "mergeStrategy": "upsert"}
            ],
            "reloadUserConfig": True,
        },
    )
    assert (
        ["codex", "-c", "override", "plugin", "list", "--marketplace", "tj-agents", "--json"],
        project.resolve(),
        45,
    ) in [call.args for call in runner.call_args_list]


@patch.object(TRUST, "run")
@patch.object(TRUST, "AppServer")
def test_hooks_still_untrusted_after_the_write_fail_and_record_nothing(
    app: mock.Mock, runner: mock.Mock, hook_state: HookTrustState
) -> None:
    state = hook_state.cwd / "state"
    listing = {
        "data": [{"cwd": str(hook_state.cwd), "errors": [], "hooks": [hook_state.hook]}]
    }
    inventory = json.dumps({"installed": [hook_state.plugin]})
    runner.side_effect = lambda arguments, cwd, *_: (
        inventory if "plugin" in arguments else hook_state.git_result(arguments, cwd)
    )
    app.return_value.__enter__.return_value.request.side_effect = (
        lambda method, params: listing if method == "hooks/list" else {}
    )
    with (
        patch.object(TRUST.Path, "home", return_value=hook_state.cwd),
        patch.object(TRUST.projects, "user_config", return_value={}),
    ):
        with pytest.raises(RuntimeError, match="still not trusted"):
            TRUST.trust_installed("codex", state)
    assert not (state / TRUST.RECEIPT).exists()


def test_batched_readback_covers_every_project_before_receipt(tmp_path, monkeypatch):
    cwds = [tmp_path / str(index) for index in range(6)]
    hook = {"source": "plugin", "enabled": True, "isManaged": False,
            "trustStatus": "untrusted", "pluginId": "base@tj-agents", "key": "key"}
    server = mock.MagicMock()
    server.__enter__.return_value = server
    calls = []

    def batches(method, parameters):
        calls.append(parameters)
        result = [{"data": [{"cwd": cwd, "hooks": [], "errors": []} for cwd in batch["cwds"]]}
                  for batch in parameters]
        if len(calls) == 1:
            result[0]["data"][0]["hooks"] = [hook]
        else:
            result[-1]["data"][-1]["errors"] = ["load failed"]
        return result

    server.requests.side_effect = batches
    monkeypatch.setattr(TRUST, "AppServer", lambda *args, **kwargs: server)
    monkeypatch.setattr(TRUST, "published_hooks", lambda: {"base@tj-agents": "digest"})
    monkeypatch.setattr(TRUST.Path, "home", lambda: cwds[0])
    monkeypatch.setattr(TRUST.projects, "user_config", dict)
    monkeypatch.setattr(TRUST.projects, "trusted", lambda config: cwds[1:])
    monkeypatch.setattr(TRUST, "run", lambda *args: '{"installed": [{"pluginId": "base@tj-agents"}]}')
    monkeypatch.setattr(TRUST, "trust_updates", lambda *args: {"key": {"trusted_hash": "hash"}})

    with pytest.raises(RuntimeError, match="could not load"):
        TRUST.trust_installed("codex", tmp_path / "state")
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert [cwd for batch in calls[1] for cwd in batch["cwds"]] == [str(cwd) for cwd in cwds]
    assert not (tmp_path / "state" / TRUST.RECEIPT).exists()


if __name__ == "__main__":
    unittest.main()
