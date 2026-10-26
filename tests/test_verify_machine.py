import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = (Path(__file__).resolve().parents[1] / ".agents" /
          "agent" / "utility" / "plugins" / "scripts" / "verify_machine.py")
spec = importlib.util.spec_from_file_location("verify_machine", SCRIPT)
verify_machine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_machine)


class VerifyMachineTests(unittest.TestCase):
    def test_reports_behavior_but_ignores_host_trust_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            claude = home / ".claude"
            codex = home / ".codex"
            claude.mkdir()
            codex.mkdir()
            (claude / "settings.json").write_text(
                '{"enabledPlugins":{"base@tj-agents":true},"permissions":{"allow":["Read"]}}',
                encoding="utf-8",
            )
            (codex / "config.toml").write_text(
                'notify = ["python", "notify.py"]\n[plugins."base@tj-agents"]\nenabled = true\n'
                '[hooks.state."trusted"]\ntrusted_hash = "sha256:x"\n'
                '[marketplaces.openai-bundled]\nsource_type = "local"\n'
                '[plugins."browser@openai-bundled"]\nenabled = true\n',
                encoding="utf-8",
            )
            agents = codex / "agents"
            agents.mkdir()
            (agents / "lane-l1.toml").write_text("", encoding="utf-8")
            findings = verify_machine.audit(home)
            self.assertEqual(4, len(findings))
            self.assertFalse(any("hooks.state" in finding for finding in findings))

    def test_clean_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual([], verify_machine.audit(Path(temp)))
            self.assertEqual("missing_home", verify_machine.inspect(Path(temp) / "missing")[0]["code"])

    def test_missing_toml_support_is_reported_only_for_existing_config(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(verify_machine, "tomllib", None):
            home = Path(temp)
            repository = home / "repo"
            repository.mkdir()
            self.assertEqual([], verify_machine.inspect(home, [repository]))
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text('notify = ["x"]\n', encoding="utf-8")
            self.assertEqual(["python_3_11_required"], [item["code"] for item in verify_machine.inspect(home)])

    def test_link_detection_does_not_need_path_stat_keywords(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(verify_machine.Path, "stat", side_effect=TypeError("follow_symlinks")):
            self.assertFalse(verify_machine._linked(Path(temp)))

    def test_reports_user_hooks_but_not_trust_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            codex = home / ".codex"
            codex.mkdir()
            (codex / "config.toml").write_text(
                '[hooks.state."trusted"]\ntrusted_hash = "sha256:x"\n'
                '[hooks.custom]\ncommand = "python hook.py"\n',
                encoding="utf-8",
            )
            hooks = codex / "hooks"
            hooks.mkdir()
            (hooks / "local.py").write_text("", encoding="utf-8")
            self.assertEqual(2, len(verify_machine.audit(home)))

    def test_parses_alternate_toml_syntax_and_named_profiles(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            codex = home / ".codex"
            codex.mkdir()
            (codex / "config.toml").write_text(
                'plugins = { "browser@openai-bundled" = { enabled = true } }\n'
                'marketplaces = { "openai-primary-runtime" = { source = "builtin" } }\n'
                'hooks = { state = { trusted = "x" } }\n'
                'agents = { reviewer = { config_file = "private-agent" } }\n'
                '[profiles.work]\nnotify = ["secret-command"]\n'
                'plugins = { "base@tj-agents" = { enabled = true } }\n'
                'marketplaces = { custom = { source = "tj-agents/core" } }\n'
                'hooks = { state = { trusted = "x" }, custom = { command = "private" } }\n'
                'model_instructions_file = "private-path"\n'
                'agent = { model = "private-model" }\n',
                encoding="utf-8",
            )
            findings = verify_machine.inspect(home)
            self.assertEqual(
                ["agents", "profiles.work.notify", "profiles.work.plugins", "profiles.work.marketplaces",
                 "profiles.work.hooks", "profiles.work.model_instructions_file", "profiles.work.agents"],
                [finding["setting"] for finding in findings],
            )
            self.assertNotIn("private", str(findings))

    def test_invalid_inputs_are_redacted_and_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".codex").mkdir()
            (home / ".claude").mkdir()
            (home / ".codex" / "config.toml").write_text('notify = "secret"\nnotify = "secret"\n', encoding="utf-8")
            (home / ".claude" / "settings.json").write_text('{"secret": bad}', encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(1, verify_machine.main(["--home", str(home), "--json"]))
            result = json.loads(output.getvalue())
            self.assertEqual("drift", result["status"])
            self.assertEqual(["invalid_config", "invalid_config"], [item["code"] for item in result["findings"]])
            self.assertNotIn("secret", output.getvalue())

    def test_read_errors_are_findings(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            with mock.patch.object(verify_machine.Path, "read_text", side_effect=PermissionError()):
                findings = verify_machine.inspect(home)
            self.assertEqual(3, sum(item["code"] == "unreadable_config" for item in findings))
            (home / ".codex" / "skills").mkdir(parents=True)
            with mock.patch.object(verify_machine.Path, "iterdir", side_effect=PermissionError()):
                findings = verify_machine.inspect(home)
            self.assertTrue(any(
                item["code"] == "unreadable_directory" and Path(item["path"]) == home / ".codex" / "skills"
                for item in findings
            ))

    def test_malformed_profiles_and_bom(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".codex").mkdir()
            (home / ".claude").mkdir()
            (home / ".codex" / "config.toml").write_text('profiles = { work = "invalid" }', encoding="utf-8-sig")
            (home / ".claude" / "settings.json").write_text('{"enabledPlugins":{"custom":true}}', encoding="utf-8-sig")
            findings = verify_machine.inspect(home)
            self.assertEqual(["user_behavior", "invalid_config"], [item["code"] for item in findings])
            self.assertEqual("profiles.work", findings[1]["setting"])

    def test_hidden_loose_files_and_system_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            skills = home / ".codex" / "skills"
            skills.mkdir(parents=True)
            (skills / ".system").mkdir()
            (skills / ".personal").mkdir()
            (skills / ".hidden.py").write_text("", encoding="utf-8")
            findings = verify_machine.inspect(home)
            self.assertEqual(2, len(findings))
            self.assertTrue(all(item["code"] == "user_loose" for item in findings))

    def test_repository_sources_and_cli_do_not_change_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir()
            repo = root / "project"
            (repo / ".codex" / "agents").mkdir(parents=True)
            (repo / ".claude").mkdir()
            (repo / ".codex" / "agents" / "local.toml").write_text("", encoding="utf-8")
            codex = repo / ".codex" / "config.toml"
            codex.write_text(
                '[marketplaces.github]\nsource = "tj-agents/core"\n'
                '[profiles.local.marketplaces.custom]\nsource_type = "local"\nsource = "../standards"\n',
                encoding="utf-8",
            )
            claude = repo / ".claude" / "settings.local.json"
            claude.write_text(
                '{"extraKnownMarketplaces":{"github":{"source":{"source":"github","repo":"tj-agents/core",'
                '"subpath":"../package","description":"file:///metadata"}},'
                '"local":{"source":{"source":"file:///tmp/standards"}}}}',
                encoding="utf-8",
            )
            before = (codex.read_bytes(), claude.read_bytes())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(1, verify_machine.main(
                    ["--home", str(home), "--repository", str(repo), "--json"]))
            findings = json.loads(output.getvalue())["findings"]
            self.assertEqual(2, sum(item["code"] == "repository_local_source" for item in findings))
            self.assertIn("profiles.local.marketplaces", [item["setting"] for item in findings])
            self.assertEqual(1, sum(item["code"] == "repository_loose" for item in findings))
            self.assertEqual(before, (codex.read_bytes(), claude.read_bytes()))
            self.assertEqual("missing_repository", verify_machine.inspect(home, [root / "missing"])[0]["code"])

    def test_remote_source_kind_does_not_hide_local_origin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir()
            repo = root / "project"
            (repo / ".codex").mkdir(parents=True)
            (repo / ".claude").mkdir()
            (repo / ".codex" / "config.toml").write_text(
                '[marketplaces.local]\nsource_type = "git"\nsource = "file:///C:/standards"\n'
                '[marketplaces.remote]\nsource_type = "github"\nrepository = "tj-agents/core"\n'
                'path = "../package"\n',
                encoding="utf-8",
            )
            (repo / ".claude" / "settings.json").write_text(
                '{"extraKnownMarketplaces":{'
                '"remote":{"source":"github","repository":"tj-agents/core","path":"../package"},'
                '"local":{"source_type":"git","url":"../standards"}}}',
                encoding="utf-8",
            )
            findings = verify_machine.inspect(home, [repo])
            self.assertEqual(
                [("repository_local_source", "marketplaces"),
                 ("repository_local_source", "extraKnownMarketplaces")],
                [(item["code"], item["setting"]) for item in findings],
            )

    def test_links_are_reported_without_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            target = Path(temp) / "target"
            (home / ".codex").mkdir(parents=True)
            target.mkdir()
            (target / "secret.toml").write_text("", encoding="utf-8")
            try:
                (home / ".codex" / "agents").symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory links unavailable")
            findings = verify_machine.inspect(home)
            self.assertEqual("linked_path", findings[0]["code"])
            self.assertEqual(home / ".codex" / "agents", Path(findings[0]["path"]))

    def test_junction_detection_uses_reparse_attribute(self):
        with mock.patch.object(verify_machine.os, "lstat") as lstat_call:
            lstat_call.return_value.st_mode = verify_machine.stat.S_IFDIR
            lstat_call.return_value.st_file_attributes = verify_machine.stat.FILE_ATTRIBUTE_REPARSE_POINT
            self.assertTrue(verify_machine._linked(Path("junction")))


if __name__ == "__main__":
    unittest.main()
