import contextlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

if sys.version_info < (3, 11):
    raise unittest.SkipTest("install requires Python 3.11 or newer")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))
import install  # noqa: E402
import tomllib  # noqa: E402

BOM = b"\xef\xbb\xbf"


def snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FakeUserEnvironment:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.broadcasts = 0

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def broadcast(self):
        self.broadcasts += 1


class Machine(unittest.TestCase):
    def test_posix_install_supplies_the_interpreter_named_by_claude_hooks(self):
        context = self.context(windows=False)
        self.converge(step('launch-check runtime'), context)
        alias = context.state_directory / 'bin' / 'python'
        self.assertEqual(install.python_alias(), alias.read_text(encoding='utf-8'))
        self.assertIn(str(alias.parent), install.posix_block(sys.executable, install.launch_script(context)))
        if os.name != 'nt':
            result = subprocess.run([str(alias), '-c', 'print("hook-interpreter-ready")'],
                                    capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual('hook-interpreter-ready', result.stdout.strip())

    def setUp(self):
        environment = mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        temp = tempfile.TemporaryDirectory(prefix="install machine ü ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.checkout = self.root / "main checkout"
        (self.checkout / "shell").mkdir(parents=True)
        (self.checkout / "shell" / "profile.ps1").write_text("# profile\n", encoding="utf-8")
        self.documents = self.home / "Documents"
        self.calls = []

    def context(self, windows=True, run=None, which=None, user_environment=None, **environ):
        return install.Context(
            home=self.home,
            project=self.checkout,
            environ=dict(environ),
            windows=windows,
            run=run or self.unexpected_run,
            which=which or (lambda name: None),
            documents=self.documents,
            user_environment=user_environment,
        )

    def unexpected_run(self, command, **kwargs):
        raise AssertionError(f"unexpected command {command}")

    def converge(self, step, context):
        out = io.StringIO()
        self.assertEqual(1, install.run_steps([step], context, True, out, io.StringIO()), out.getvalue())
        self.assertEqual(0, install.run_steps([step], context, False, out, io.StringIO()), out.getvalue())
        self.assertEqual([], step.check(context))
        before = snapshot(self.root)
        applied = mock.Mock(side_effect=step.apply)
        rerun = install.Step(step.name, step.check, applied, step.skip)
        self.assertEqual(0, install.run_steps([rerun], context, False, io.StringIO(), io.StringIO()))
        applied.assert_not_called()
        self.assertEqual(before, snapshot(self.root))
        self.assertEqual(0, install.run_steps([rerun], context, True, io.StringIO(), io.StringIO()))


def step(name):
    return next(item for item in install.STEPS if item.name == name)


class ProfileTests(Machine):
    def profiles(self):
        return [self.documents / name / "Microsoft.PowerShell_profile.ps1" for name in install.PROFILE_NAMES]

    def script(self):
        return self.home / ".agents-state" / "bin" / ".agents" / "agent" / "runtime" / "startup_binding.py"

    def block(self, newline="\r\n", loader=None):
        loader = loader or install.profile_loader(self.context())
        return install.powershell_block(install.plugin_autoupdate.console_python(), self.script(), loader, newline)

    def test_missing_profiles_are_created_with_the_block(self):
        self.converge(step("shell profiles"), self.context())
        for path in self.profiles():
            self.assertEqual(BOM + (self.block() + "\r\n").encode("utf-8"), path.read_bytes())
            self.assertEqual([], list(path.parent.glob("*.bak")))

    def test_host_launchers_bind_the_current_project_and_native_target_without_a_legacy_flag(self):
        powershell = self.block()
        posix = install.posix_block(sys.executable, self.script())
        for block in (powershell, posix):
            self.assertIn("startup_binding.py", block)
            self.assertIn("--target", block)
            self.assertIn("--require-session-start", block)
            self.assertNotIn("--launch", block)
        self.assertIn("--project (Get-Location).Path", powershell)
        self.assertIn('--project "$PWD"', posix)

    def test_an_existing_block_for_another_checkout_is_rewired(self):
        path = self.profiles()[1]
        path.parent.mkdir(parents=True)
        path.write_text("first\n" + self.block("\n", Path("C:/worktree/shell/profile.ps1")) + "\nlast\n",
                        encoding="utf-8")
        install.apply_profile(self.context())
        self.assertEqual("first\n" + self.block("\n") + "\nlast\n", path.read_text(encoding="utf-8"))

    def test_duplicate_managed_blocks_collapse_to_one(self):
        path = self.profiles()[1]
        path.parent.mkdir(parents=True)
        stale = install.PROFILE_START + "\r\nold\r\n" + install.PROFILE_END
        path.write_text("a\r\n" + stale + "\r\nb\r\n" + stale + "\r\nc\r\n", encoding="utf-8", newline="")
        install.apply_profile(self.context())
        text = path.read_bytes().decode("utf-8")
        self.assertEqual(1, text.count(install.PROFILE_START))
        self.assertEqual("a\r\n" + self.block() + "\r\nb\r\nc\r\n", text)

    def test_malformed_markers_are_rejected_without_writing(self):
        path = self.profiles()[1]
        path.parent.mkdir(parents=True)
        path.write_text(install.PROFILE_START + "\nno end\n", encoding="utf-8")
        before = snapshot(self.root)
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(2, install.run_steps([step("shell profiles")], self.context(), False, out, err))
        self.assertIn("Malformed", err.getvalue())
        self.assertEqual(before, snapshot(self.root))

    def test_an_undecodable_profile_is_skipped_with_a_reason_and_the_others_still_wire(self):
        windows, core = self.profiles()
        windows.parent.mkdir(parents=True)
        core.parent.mkdir(parents=True)
        undecodable = b"$x = \xe9\r\n"
        windows.write_bytes(undecodable)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(0, install.run_steps([step("shell profiles")], self.context(), False, out, err))

        self.assertEqual(undecodable, windows.read_bytes())
        self.assertEqual(BOM + (self.block() + "\r\n").encode("utf-8"), core.read_bytes())
        self.assertIn(f"warning shell profiles: {windows}: cannot decode as UTF-8 or UTF-16", err.getvalue())

    def test_an_undecodable_profile_reports_a_warning_not_drift(self):
        windows, core = self.profiles()
        windows.parent.mkdir(parents=True)
        core.parent.mkdir(parents=True)
        core.write_bytes(BOM + (self.block() + "\r\n").encode("utf-8"))
        undecodable = b"$x = \xe9\r\n"
        windows.write_bytes(undecodable)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(0, install.run_steps([step("shell profiles")], self.context(), True, out, err))

        self.assertIn("ok     shell profiles", out.getvalue())
        self.assertNotIn("drift", out.getvalue())
        self.assertIn(f"warning shell profiles: {windows}: cannot decode as UTF-8 or UTF-16", err.getvalue())

    def test_utf16_bom_profiles_are_decoded_and_written_back_in_the_same_encoding(self):
        windows, core = self.profiles()
        windows.parent.mkdir(parents=True)
        core.parent.mkdir(parents=True)
        windows.write_bytes(install.codecs.BOM_UTF16_LE + "Write-Output 'keep-me'\r\n".encode("utf-16-le"))

        self.converge(step("shell profiles"), self.context())

        expected = install.codecs.BOM_UTF16_LE + ("Write-Output 'keep-me'\r\n\r\n" + self.block() + "\r\n").encode("utf-16-le")
        self.assertEqual(expected, windows.read_bytes())

    def test_linux_wires_only_existing_profiles_and_never_loads_the_windows_profile(self):
        zshrc = self.home / ".zshrc"
        zshrc.write_text("export A=1", encoding="utf-8")
        powershell = self.home / ".config" / "powershell" / "Microsoft.PowerShell_profile.ps1"
        powershell.parent.mkdir(parents=True)
        powershell.write_bytes(("keep\n" + install.PROFILE_START + "\nold\n" + install.PROFILE_END + "\n").encode("utf-8"))
        python = install.plugin_autoupdate.console_python()

        self.converge(step("shell profiles"), self.context(windows=False))

        self.assertFalse((self.home / ".bashrc").exists())
        self.assertEqual("export A=1\n\n" + install.posix_block(python, self.script()) + "\n",
                         zshrc.read_text(encoding="utf-8"))
        self.assertEqual(("keep\n" + install.powershell_block(python, self.script(), None, "\n") + "\n").encode("utf-8"),
                         powershell.read_bytes())
        self.assertNotIn("profile.ps1", powershell.read_text(encoding="utf-8"))

    def test_posix_block_escapes_single_quotes_in_paths(self):
        block = install.posix_block("/opt/it's/python3", PurePosixPath("/home/o'neil/launch_check.py"))
        self.assertIn("'/opt/it'\\''s/python3'", block)
        self.assertIn("'/home/o'\\''neil/launch_check.py'", block)

    def test_linux_creates_bashrc_when_no_profile_exists(self):
        self.converge(step("shell profiles"), self.context(windows=False))
        self.assertEqual([self.home / ".bashrc"],
                         [path for path in install.profile_paths(self.context(windows=False)) if path.exists()])


class HiddenBashTests(Machine):
    def setUp(self):
        super().setUp()
        (self.home / ".agents-state/bin").mkdir(parents=True)
        (self.checkout / "shell" / "hidden-bash.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        self.real = self.root / "Git" / "bash.exe"
        self.real.parent.mkdir()
        self.real.write_text("", encoding="utf-8")
        self.environment = FakeUserEnvironment({"TJ_AGENTS_REAL_GIT_BASH": str(self.real)})
        self.target = "x86_64-w64-mingw32\n"

    def test_gui_flags_follow_the_compiler_target(self):
        for target, flags in (("x86_64-w64-mingw32\n", ["-municode", "-mwindows"]),
                              ("x86_64-pc-windows-msvc\n", ["-Wl,/subsystem:windows"])):
            with self.subTest(target=target):
                self.target = target
                self.calls.clear()
                install.build_hidden_bash(self.context(run=self.run_compiler, which=lambda name: name), str(self.real))
                self.assertEqual(["clang.exe", "-O2", *flags], self.calls[0][:2 + len(flags)])

    def run_compiler(self, command, **kwargs):
        if command[1:] == ["-dumpmachine"]:
            return completed(0, self.target)
        self.calls.append(command)
        if command[0] == "clang.exe":
            Path(command[-1]).write_bytes(b"proxy")
            return completed()
        self.assertEqual(str(self.real), kwargs["env"]["TJ_AGENTS_REAL_GIT_BASH"])
        return completed(73, "HIDDEN_BASH_OK")

    def test_the_proxy_is_built_once_and_the_user_environment_is_set(self):
        context = self.context(run=self.run_compiler, which=lambda name: name if name == "clang.exe" else None,
                               user_environment=self.environment)
        self.converge(step("hidden Git Bash for Claude"), context)
        proxy = self.home / ".agents-state/bin/hidden-bash.exe"
        self.assertEqual(b"proxy", proxy.read_bytes())
        self.assertFalse((self.home / ".agents-state/bin/hidden-bash.build.exe").exists())
        self.assertEqual(str(proxy), self.environment.values["CLAUDE_CODE_GIT_BASH_PATH"])
        self.assertEqual(1, self.environment.broadcasts)
        self.assertEqual(2, len(self.calls))

    def test_a_newer_source_rebuilds_the_proxy(self):
        proxy = self.home / ".agents-state/bin/hidden-bash.exe"
        proxy.write_bytes(b"old")
        os.utime(proxy, (1, 1))
        context = self.context(run=self.run_compiler, which=lambda name: name if name == "clang.exe" else None,
                               user_environment=self.environment)
        self.assertIn("build shell/hidden-bash.exe", install.check_hidden_bash(context))

    def test_a_failed_probe_keeps_the_previous_proxy(self):
        def broken(command, **kwargs):
            if command[1:] == ["-dumpmachine"]:
                return completed(0, self.target)
            if command[0] == "clang.exe":
                Path(command[-1]).write_bytes(b"broken")
                return completed()
            return completed(1, "")

        context = self.context(run=broken, which=lambda name: "clang.exe" if name == "clang.exe" else None,
                               user_environment=self.environment)
        with self.assertRaises(install.InstallError):
            install.apply_hidden_bash(context)
        self.assertFalse((self.home / ".agents-state/bin/hidden-bash.exe").exists())
        self.assertFalse((self.home / ".agents-state/bin/hidden-bash.build.exe").exists())

    def test_rebuilding_renames_the_running_proxy_aside_instead_of_replacing_it(self):
        proxy = self.home / ".agents-state/bin/hidden-bash.exe"
        proxy.write_bytes(b"old")
        build = proxy.with_name("hidden-bash.build.exe")
        build.write_bytes(b"new")

        install.install_hidden_bash(build, proxy)

        self.assertEqual(b"new", proxy.read_bytes())
        self.assertFalse(build.exists())
        first_asides = install.hidden_bash_stale_asides(proxy)
        self.assertEqual(1, len(first_asides))
        self.assertEqual(b"old", first_asides[0].read_bytes())

        second_build = proxy.with_name("hidden-bash.build.exe")
        second_build.write_bytes(b"newer")
        install.install_hidden_bash(second_build, proxy)

        self.assertEqual(b"newer", proxy.read_bytes())
        second_asides = install.hidden_bash_stale_asides(proxy)
        self.assertEqual(1, len(second_asides))
        self.assertEqual(b"new", second_asides[0].read_bytes())
        self.assertNotEqual(first_asides[0], second_asides[0])

    def test_a_stale_aside_that_cannot_be_deleted_is_ignored(self):
        proxy = self.home / ".agents-state/bin/hidden-bash.exe"
        proxy.write_bytes(b"old")
        stuck = proxy.with_name(f"{proxy.stem}.stale-stuck{proxy.suffix}")
        stuck.mkdir()
        build = proxy.with_name("hidden-bash.build.exe")
        build.write_bytes(b"new")

        install.install_hidden_bash(build, proxy)

        self.assertEqual(b"new", proxy.read_bytes())
        self.assertTrue(stuck.is_dir())

    def test_a_failed_install_restores_the_moved_aside_proxy(self):
        proxy = self.home / ".agents-state/bin/hidden-bash.exe"
        proxy.write_bytes(b"old")
        build = proxy.with_name("hidden-bash.build.exe")
        build.write_bytes(b"new")
        real_replace = os.replace

        def failing_replace(source, destination):
            if Path(source) == build:
                raise OSError("disk full")
            real_replace(source, destination)

        with mock.patch.object(install.os, "replace", side_effect=failing_replace):
            with self.assertRaises(OSError):
                install.install_hidden_bash(build, proxy)

        self.assertEqual(b"old", proxy.read_bytes())
        self.assertEqual([], install.hidden_bash_stale_asides(proxy))

    def test_hidden_bash_is_windows_only(self):
        out = io.StringIO()
        install.run_steps([step("hidden Git Bash for Claude")], self.context(windows=False), False, out)
        self.assertIn("n/a", out.getvalue())


class InstallerStepTests(Machine):
    def installed_engineering(self):
        package = self.root / "cache" / "tj-agents" / "engineering" / "1"
        installer = package / "scripts" / "install-codex-agents.ps1"
        installer.parent.mkdir(parents=True)
        installer.write_text("", encoding="utf-8")
        claude = self.home / ".claude"
        (claude / "plugins").mkdir(parents=True)
        (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"engineering@tj-agents": True}}),
                                              encoding="utf-8")
        (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"plugins": {"engineering@tj-agents": [{"installPath": str(package)}]}}), encoding="utf-8")
        return installer

    def test_codex_agents_are_delivered_with_pwsh_from_the_installed_engineering_package(self):
        installer = self.installed_engineering()
        codex = self.home / ".codex"
        codex.mkdir()
        state = {"installed": False}

        def run(command, **kwargs):
            self.assertEqual(["pwsh-path", "-NoProfile", "-NonInteractive", "-File", str(installer), "-CodexHome", str(codex)],
                             command[:-1])
            if command[-1] == "-Verify":
                return completed(0 if state["installed"] else 1, "", "Profile verification failed: missing")
            state["installed"] = True
            return completed(0, "INSTALLED: 12 change(s)\n")

        context = self.context(windows=False, run=run, which=lambda name: "pwsh-path" if name == "pwsh" else None)
        self.converge(step("Codex profile agents"), context)

    def test_codex_agents_skip_without_pwsh(self):
        out = io.StringIO()
        install.run_steps([step("Codex profile agents")], self.context(), False, out)
        self.assertIn("n/a    Codex profile agents: pwsh is not installed", out.getvalue())

    def test_codex_agents_skip_without_an_installed_engineering_package(self):
        (self.home / ".codex").mkdir()
        out = io.StringIO()
        install.run_steps([step("Codex profile agents")], self.context(which=lambda name: "pwsh-path"), False, out)
        self.assertIn("n/a    Codex profile agents: no installed engineering plugin", out.getvalue())


class PermissionStepTests(Machine):
    def test_dead_owned_rules_converge(self):
        claude = self.home / ".claude"
        state = self.home / ".agents-state"
        package = self.root / "cache" / "tj-agents" / "engineering" / "1"
        package.mkdir(parents=True)
        (package / "harness.json").write_text(json.dumps({"schema_version": 1, "plugin": "tj-agents/engineering",
            "source_roots": [], "requires": {"marketplaces": [], "plugins": [], "hooks": [],
            "permissions": {"claude_allow": ["Bash(shipped)"], "codex_prefix_rules": []}}}), encoding="utf-8")
        (claude / "plugins").mkdir(parents=True)
        (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"plugins": {"engineering@tj-agents": [{"installPath": str(package)}]}}), encoding="utf-8")
        (claude / "plugins" / "known_marketplaces.json").write_text(json.dumps(
            {"tj-agents": {"source": {"source": "github", "repo": "tj-agents/marketplace"}}}), encoding="utf-8")
        dead = "Bash(python -B C:/x/.claude/plugins/cache/base-agents/engineering/1/hooks/gate.py)"
        settings = claude / "settings.json"
        settings.write_text(json.dumps({"permissions": {"allow": ["Bash(mine)", dead]}}), encoding="utf-8")
        state.mkdir()
        (state / "harness-permissions.json").write_text(json.dumps(
            {"schema_version": 2, "claude": {str(settings): [dead]}, "codex": {}}), encoding="utf-8")

        self.converge(step("harness permissions"), self.context(windows=False))

        self.assertEqual(["Bash(mine)", "Bash(shipped)"], json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"])

    def test_a_missing_permission_sync_module_is_reported(self):
        context = install.Context(home=self.home, project=self.checkout, environ={}, windows=False,
                                run=self.unexpected_run, which=lambda name: None, documents=self.documents,
                                root=self.root / "no such runtime root")

        with self.assertRaises(install.InstallError):
            install.permission_sync(context)


class RuntimeTests(Machine):
    def installed_receipt(self, context):
        return json.loads((context.state_directory / "bin" / "source.json").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("powershell.exe") or shutil.which("pwsh"), "PowerShell shell ownership check")
    def test_agent_profile_preserves_cli_owned_session_search_and_navigation(self):
        loader = install.ROOT / ".agents/agent/resources/shell/profile.ps1"
        quoted = str(loader).replace("'", "''")
        script = (
            "function global:cr { 'CLI_CR' }; function global:f { 'CLI_F' }; "
            f". '{quoted}'; "
            "if ((Get-Command cr).Definition -notmatch 'CLI_CR' -or "
            "(Get-Command f).Definition -notmatch 'CLI_F') { exit 9 }; "
            "Write-Output 'cli-commands-preserved'"
        )
        result = subprocess.run([shutil.which("powershell.exe") or shutil.which("pwsh"), "-NoProfile", "-Command", script],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("cli-commands-preserved", result.stdout)

    def test_packaged_install_and_fresh_shell_survive_removing_the_source_checkout(self):
        source = self.root / "published package"
        shutil.copytree(install.ROOT, source)
        context = self.context(windows=os.name == "nt")
        context.root = source
        install.apply_runtime(context)
        install.apply_profile(context)
        shutil.rmtree(source)
        shutil.rmtree(self.checkout)
        loader = context.state_directory / "bin/.agents/agent/resources/shell/profile.ps1"
        self.assertTrue(loader.is_file())
        if context.windows:
            profile = install.profile_paths(context)[0]
            quoted = str(profile).replace("'", "''")
            command = ["powershell.exe", "-NoProfile", "-Command",
                       f". '{quoted}'; (Get-Command codex).Definition"]
        else:
            command = ["bash", "--noprofile", "--norc", "-c", 'source "$1"; declare -f codex',
                       "fixture", str(context.home / ".bashrc")]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("startup_binding.py", result.stdout)
        self.assertNotIn(str(source), result.stdout)
        self.assertEqual([], install.check_profile(context))

    def test_native_permission_defaults_do_not_require_an_installer_checkout(self):
        context = self.context(windows=False)
        context.project = self.root / "consumer"
        context.project.mkdir()
        with mock.patch.object(install.host_permissions, "permissions", return_value="standard") as mode:
            planned = install.native_permissions_plan(context)
        mode.assert_called_once_with(context.project)
        self.assertTrue(planned)

    def test_installed_runtime_preserves_the_agent_package_closure_without_source_checkout(self):
        context = self.context(windows=False)
        self.converge(step("launch-check runtime"), context)
        installed = self.home / ".agents-state/bin"
        self.assertEqual(install.runtime_receipt(install.ROOT), self.installed_receipt(context))
        for relative in (Path('.agents/agent/runtime/startup_binding.py'),
                         Path('.agents/agent/runtime/install.py'),
                         Path('.agents/agent/runtime/tj_agent/content.py'),
                         Path('.agents/agent/runtime/tj_agent/plugins/sources.py'),
                         Path('.agents/agent/resources/lanes/resolve.py'),
                         Path('.agents/agent/utility/peer/scripts/register_session.py'),
                         Path('.agents/agent-store/runtime/session_store.py')):
            self.assertTrue((installed / relative).is_file(), relative)
        result = subprocess.run([sys.executable, "-I", "-B", str(install.launch_script(context)), "--help"],
                                cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('--require-session-start', result.stdout)

    def test_the_agent_runtime_receipt_and_exports_are_installed_once(self):
        context = self.context(windows=False)
        self.converge(step("launch-check runtime"), context)
        installed = context.state_directory / 'bin'
        for name in install.runtime_receipt(install.ROOT)["files"]:
            self.assertEqual((install.ROOT / name).read_bytes(), (installed / name).read_bytes())

    def test_a_root_without_the_runtime_exports_fails_closed_without_writing(self):
        empty = self.root / "empty root"
        empty.mkdir()
        context = self.context(windows=False)
        context.root = empty
        before = snapshot(self.root)
        with self.assertRaises(install.InstallError):
            install.check_runtime(context)
        self.assertEqual(before, snapshot(self.root))

    def test_installed_shell_entry_point_resolves_its_complete_dependency_closure(self):
        context = self.context(windows=os.name == 'nt')
        install.apply_runtime(context)
        result = subprocess.run([sys.executable, '-I', '-B', str(install.launch_script(context)), '--help'],
                                cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('--require-session-start', result.stdout)

    def test_runtime_check_detects_export_drift_without_writing_and_preserves_unrelated_files(self):
        context = self.context(windows=False)
        install.apply_runtime(context)
        installed = context.state_directory / 'bin'
        unrelated = installed / 'keep-me.txt'
        unrelated.write_text('keep', encoding='utf-8')
        bot_identity = installed / 'bot-identity' / 'identity.json'
        bot_identity.parent.mkdir()
        bot_identity.write_text('keep', encoding='utf-8')
        target = install.launch_script(context)
        target.write_text('changed', encoding='utf-8')
        before = snapshot(self.root)
        changes = install.check_runtime(context)
        self.assertIn('install Agent runtime: managed runtime file differs: '
                      '.agents/agent/runtime/startup_binding.py', changes)
        self.assertEqual({'install Agent runtime: managed runtime file differs: '
                          '.agents/agent/runtime/startup_binding.py'}, set(changes))
        self.assertEqual(before, snapshot(self.root))
        install.apply_runtime(context)
        self.assertTrue(unrelated.is_file())
        self.assertTrue(bot_identity.is_file())
        self.assertEqual(install.python_alias(), (installed / 'python').read_text(encoding='utf-8'))

    def test_unowned_or_linked_python_alias_is_refused_without_writing(self):
        context = self.context(windows=False)
        install.apply_runtime(context)
        alias = context.state_directory / 'bin' / 'python'
        alias.write_text('custom', encoding='utf-8')
        before = snapshot(self.root)
        self.assertIn('refuse unowned', install.python_alias_status(context, alias.parent))
        with self.assertRaises(install.InstallError):
            install.install_python_alias(context, alias.parent)
        self.assertEqual(before, snapshot(self.root))

    def test_managed_python_alias_updates_when_the_interpreter_changes(self):
        context = self.context(windows=False)
        alias = context.state_directory / 'bin' / 'python'
        with mock.patch.object(install.sys, 'executable', '/managed/python-a'):
            install.apply_runtime(context)
            self.assertEqual(install.python_alias('/managed/python-a'), alias.read_text(encoding='utf-8'))
        with mock.patch.object(install.sys, 'executable', '/managed/python-b'):
            self.assertIn('install Claude hook interpreter', install.python_alias_status(context, alias.parent))
            install.apply_runtime(context)
            self.assertEqual(install.python_alias('/managed/python-b'), alias.read_text(encoding='utf-8'))

    def test_managed_python_alias_upgrade_preserves_an_unowned_temporary_collision(self):
        context = self.context(windows=False)
        alias = context.state_directory / 'bin' / 'python'
        with mock.patch.object(install.sys, 'executable', '/managed/python-a'):
            install.apply_runtime(context)
        collision = alias.with_name('.python.collision.tmp')
        collision.write_text('custom', encoding='utf-8')
        before = snapshot(self.root)
        with mock.patch.object(install.sys, 'executable', '/managed/python-b'), \
             mock.patch.object(install.secrets, 'token_hex', return_value='collision'):
            with self.assertRaises(FileExistsError):
                install.install_python_alias(context, alias.parent)
        self.assertEqual(install.python_alias('/managed/python-a'), alias.read_text(encoding='utf-8'))
        self.assertEqual('custom', collision.read_text(encoding='utf-8'))
        self.assertEqual(before, snapshot(self.root))

    def test_modified_managed_python_alias_is_refused_without_writing(self):
        context = self.context(windows=False)
        install.apply_runtime(context)
        alias = context.state_directory / 'bin' / 'python'
        alias.write_text(install.python_alias() + '# modified\n', encoding='utf-8')
        before = snapshot(self.root)
        self.assertIn('refuse unowned', install.python_alias_status(context, alias.parent))
        with self.assertRaises(install.InstallError):
            install.install_python_alias(context, alias.parent)
        self.assertEqual(before, snapshot(self.root))

    def test_python_alias_symlink_refuses_without_touching_outside_file(self):
        context = self.context(windows=False)
        install.apply_runtime(context)
        alias = context.state_directory / 'bin' / 'python'
        outside = self.root / 'outside-python'
        outside.write_text('custom', encoding='utf-8')
        alias.unlink()
        try:
            alias.symlink_to(outside)
        except OSError as error:
            self.skipTest(f'symlink creation is unavailable: {error}')
        before = snapshot(self.root)
        with self.assertRaises(install.InstallError):
            install.install_python_alias(context, alias.parent)
        self.assertEqual('custom', outside.read_text(encoding='utf-8'))
        self.assertEqual(before, snapshot(self.root))

    def test_dangling_reparse_python_alias_is_refused_before_existence_check(self):
        context = self.context(windows=False)
        alias = context.state_directory / 'bin' / 'python'
        delivery = mock.Mock()
        delivery.is_reparse.side_effect = lambda path: path == alias
        with mock.patch.object(install, 'runtime_delivery', delivery), \
             mock.patch.object(install.os.path, 'lexists', side_effect=lambda path: Path(path) == alias):
            with self.assertRaisesRegex(install.InstallError, 'interpreter is a link'):
                install.python_alias_path(context, alias.parent)
        delivery.is_reparse.assert_called_once_with(alias)


class CatchUpTests(Machine):
    rows = [
        {"nameWithOwner": "tj-agents/core", "defaultBranchRef": {"target": {"oid": "a" * 40}}},
        {"nameWithOwner": "tj-agents/old", "isArchived": True, "defaultBranchRef": {"target": {"oid": "b" * 40}}},
        {"nameWithOwner": "tj-agents/empty", "defaultBranchRef": None},
    ]

    def test_catch_up_runs_only_when_a_default_branch_moved(self):
        state = self.home / ".agents-state" / "machine-events-state.json"

        def catch_up(argv):
            install.plugin_autoupdate.write_json(state, {"tj-agents/core": {"revision": "a" * 40, "policy": None}})
            return 0

        with mock.patch.object(install.machine_events, "catch_up_states", lambda api: iter(self.rows)), \
                mock.patch.object(install.machine_events, "main", side_effect=catch_up) as main, \
                mock.patch.object(install.launch_check, "mark_current", return_value=True) as mark:
            self.converge(step("plugin and merge-policy catch-up"), self.context(windows=False))
        main.assert_called_once_with([])
        mark.assert_called_once_with()

    def test_catch_up_failures_fail_the_step(self):
        err = io.StringIO()
        with mock.patch.object(install.machine_events, "catch_up_states", lambda api: iter(self.rows)), \
                mock.patch.object(install.machine_events, "main", return_value=1), \
                mock.patch.object(install.launch_check, "mark_current", side_effect=OSError("offline")):
            self.assertEqual(2, install.run_steps([step("plugin and merge-policy catch-up")], self.context(windows=False),
                                                False, io.StringIO(), err))
        self.assertIn("machine-events.log", err.getvalue())


class AutoUpdateTests(Machine):
    def test_claude_marketplace_auto_update_converges(self):
        claude = self.home / ".claude"
        (claude / "plugins").mkdir(parents=True)
        (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"base@tj-agents": True}}), encoding="utf-8")
        (claude / "plugins" / "known_marketplaces.json").write_text(json.dumps({"tj-agents": {"autoUpdate": False}}),
                                                                       encoding="utf-8")
        self.converge(step("Claude marketplace auto-update"), self.context(windows=False))


class ChannelTests(Machine):
    def test_the_channel_converges_and_a_rerun_writes_nothing_to_github(self):
        variables = {"tj-agents/core": "a" * 48}
        stored = set()
        writes = []

        def gh(arguments, payload=None):
            if arguments[0] == "--method":
                writes.append(arguments)
                repository = arguments[2].split("/actions/")[0].removeprefix("repos/")
                variables[repository] = payload["value"]
                return None
            repository = arguments[0].split("/actions/")[0].removeprefix("repos/")
            if "/actions/variables" in arguments[0]:
                return {"variables": [{"name": "MACHINE_EVENTS_TOPIC", "value": variables[repository]}]
                        if repository in variables else []}
            return {"secrets": [{"name": "MACHINE_EVENTS_TOPIC"}] if repository in stored else []}

        def secret(repository, topic):
            writes.append(repository)
            stored.add(repository)

        rows = [{"nameWithOwner": "tj-agents/core"}, {"nameWithOwner": "tj-agents/other"}]
        with mock.patch.object(install.machine_events, "gh", gh), \
                mock.patch.object(install.machine_events, "catch_up_states", lambda api: iter(rows)), \
                mock.patch.object(install, "set_topic_secret", secret):
            self.converge(step("event channel and publishers"), self.context(windows=False))
        self.assertEqual("a" * 48, variables["tj-agents/other"])
        self.assertEqual(3, len(writes))


class RunStepsTests(Machine):
    def test_cli_step_selection_preserves_defaults_preconditions_and_declared_order(self):
        cases = (
            ([], [item.name for item in install.STEPS]),
            (["--step", "shell profiles"],
             ["preconditions", "launch-check runtime", "shell profiles"]),
            (["--check", "--step", "shell profiles", "--step", "launch-check runtime"],
             ["preconditions", "launch-check runtime", "shell profiles"]),
            (["--step", "plugin setup"],
             ["preconditions", "plugin and merge-policy catch-up", "harness permissions", "plugin setup"]),
        )
        for arguments, expected in cases:
            with mock.patch.object(install, "machine_context", return_value=self.context()), \
                    mock.patch.object(install, "run_steps", return_value=0) as run:
                self.assertEqual(0, install.main(arguments))
                self.assertEqual(expected, [item.name for item in run.call_args.args[0]])
                self.assertEqual("--check" in arguments, run.call_args.args[2])

    def test_scoped_install_never_checks_or_applies_unselected_publisher_settings(self):
        prerequisite = mock.Mock(return_value=[])
        installed = mock.Mock(return_value=["runtime installed"])
        unrelated = mock.Mock(side_effect=AssertionError("unselected remote operation"))
        steps = (
            install.Step("preconditions", prerequisite, unrelated, required=True),
            install.Step("event channel and publishers", unrelated, unrelated),
            install.Step("launch-check runtime", lambda context: ["runtime missing"], installed),
        )
        context = self.context()
        with mock.patch.object(install, "STEPS", steps), \
                mock.patch.object(install, "machine_context", return_value=context), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, install.main(["--step", "launch-check runtime"]))
        prerequisite.assert_called_once_with(context)
        installed.assert_called_once_with(context)
        unrelated.assert_not_called()

    def test_unknown_step_fails_before_any_machine_operation(self):
        with mock.patch.object(install, "machine_context") as context, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            install.main(["--step", "not an install step"])
        self.assertEqual(2, error.exception.code)
        context.assert_not_called()

    def legacy_machine(self):
        pass

    def file_steps(self):
        return [step(name) for name in ("shell profiles", "harness permissions")]

    def test_check_writes_nothing_and_reports_drift_until_converged(self):
        self.legacy_machine()
        context = self.context()
        before = snapshot(self.root)
        out = io.StringIO()
        self.assertEqual(1, install.run_steps(self.file_steps(), context, True, out, io.StringIO()))
        self.assertEqual(before, snapshot(self.root))
        self.assertIn("drift  shell profiles", out.getvalue())
        self.assertEqual(0, install.run_steps(self.file_steps(), context, False, io.StringIO(), io.StringIO()))
        converged = snapshot(self.root)
        self.assertEqual(0, install.run_steps(self.file_steps(), context, False, io.StringIO(), io.StringIO()))
        self.assertEqual(converged, snapshot(self.root))
        self.assertEqual(0, install.run_steps(self.file_steps(), context, True, io.StringIO(), io.StringIO()))

    def test_a_failed_step_does_not_stop_later_steps_but_fails_the_run(self):
        later = mock.Mock(return_value=[])
        failing = install.Step("failing", mock.Mock(side_effect=RuntimeError("boom")), mock.Mock())
        steps = [failing, install.Step("later", later, mock.Mock())]
        err = io.StringIO()
        self.assertEqual(2, install.run_steps(steps, self.context(), False, io.StringIO(), err))
        later.assert_called_once()
        self.assertIn("failed failing: boom", err.getvalue())

    def test_a_failed_required_step_stops_the_run(self):
        later = mock.Mock(return_value=[])
        failing = install.Step("required", mock.Mock(side_effect=RuntimeError("not tommy")), mock.Mock(), required=True)
        self.assertEqual(2, install.run_steps([failing, install.Step("later", later, mock.Mock())], self.context(), False,
                                            io.StringIO(), io.StringIO()))
        later.assert_not_called()

    def test_failed_catchup_skips_plugin_setup_even_when_scoped(self):
        catchup = install.Step("plugin and merge-policy catch-up", mock.Mock(side_effect=RuntimeError("offline")), mock.Mock())
        permissions = install.Step("harness permissions", mock.Mock(return_value=[]), mock.Mock())
        plugin = install.Step("plugin setup", mock.Mock(return_value=["needed"]), mock.Mock())
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(2, install.run_steps([catchup, permissions, plugin], self.context(), False, out, err))
        plugin.check.assert_not_called()
        plugin.apply.assert_not_called()
        self.assertIn("prerequisite install step failed", out.getvalue())

    def test_windows_only_steps_skip_on_linux_without_running_anything(self):
        out = io.StringIO()
        self.assertEqual(0, install.run_steps([step("hidden Git Bash for Claude")], self.context(windows=False), True, out))
        self.assertIn("n/a    hidden Git Bash for Claude: Windows only", out.getvalue())

    def test_the_steps_run_in_the_documented_order(self):
        self.assertEqual([
            "preconditions", "native permission defaults", "event channel and publishers",
            "Claude marketplace auto-update", "launch-check runtime", "shell profiles", "hidden Git Bash for Claude",
            "Codex profile agents", "plugin and merge-policy catch-up", "harness permissions",
            "plugin setup", "background machine events",
        ], [item.name for item in install.STEPS])


class RepairTests(Machine):
    def test_repair_checks_required_steps_then_applies_only_the_named_step(self):
        prerequisite = mock.Mock(return_value=[])
        applied = mock.Mock(return_value=["converged"])
        unrelated = mock.Mock(side_effect=AssertionError("unselected step"))
        steps = (
            install.Step("preconditions", prerequisite, unrelated, required=True),
            install.Step("plugin setup", unrelated, applied),
            install.Step("launch-check runtime", unrelated, unrelated),
        )
        context = self.context()
        project = self.root / "project"
        with mock.patch.object(install, "STEPS", steps), \
                mock.patch.object(install, "machine_context", return_value=context):
            self.assertEqual(["converged"], install.repair("plugin setup", project))
        prerequisite.assert_called_once_with(context)
        applied.assert_called_once_with(context)
        unrelated.assert_not_called()
        self.assertEqual(project, install.plugin_setup_project(context))

    def test_repair_stops_when_a_required_precondition_fails(self):
        applied = mock.Mock()
        steps = (
            install.Step("preconditions", mock.Mock(side_effect=RuntimeError("not tommy")), mock.Mock(), required=True),
            install.Step("plugin setup", mock.Mock(), applied),
        )
        with mock.patch.object(install, "STEPS", steps), \
                mock.patch.object(install, "machine_context", return_value=self.context()), \
                self.assertRaisesRegex(RuntimeError, "not tommy"):
            install.repair("plugin setup", self.root)
        applied.assert_not_called()

    def test_repair_rejects_an_unknown_step(self):
        with mock.patch.object(install, "machine_context") as context, \
                self.assertRaisesRegex(install.InstallError, "unknown install step"):
            install.repair("not an install step", self.root)
        context.assert_not_called()


class NativePermissionsTests(Machine):
    @mock.patch.object(install.host_permissions, "desktop_running", return_value=False)
    def test_native_permissions_follow_portable_mode_and_converge_for_both_platforms(self, _desktop):
        settings = self.checkout / ".agents/settings.json"
        settings.parent.mkdir()
        for windows in (True, False):
            context = self.context(windows=windows)
            for mode in ("bypass", "standard", "bypass"):
                with self.subTest(windows=windows, mode=mode):
                    settings.write_text(json.dumps({"configuration": {"core": {"permissions": mode}}}), encoding="utf-8")
                    app_state = context.codex_home / ".codex-global-state.json"
                    app_state.parent.mkdir(parents=True, exist_ok=True)
                    app_state.write_text(json.dumps({"electron-persisted-atom-state": {
                        "agent-mode-by-host-id": {"local": "guardian-approvals", "remote": "read-only"}}}), encoding="utf-8")
                    before = snapshot(self.root)
                    install.check_native_permissions(context)
                    self.assertEqual(before, snapshot(self.root))
                    install.apply_native_permissions(context)
                    self.assertEqual([], install.check_native_permissions(context))
                    converged = snapshot(self.root)
                    self.assertEqual([], install.apply_native_permissions(context))
                    self.assertEqual(converged, snapshot(self.root))
                    claude = json.loads((context.claude_home / "settings.json").read_text(encoding="utf-8"))
                    codex = tomllib.loads((context.codex_home / "config.toml").read_text(encoding="utf-8"))
                    self.assertEqual("bypassPermissions" if mode == "bypass" else "default", claude["permissions"]["defaultMode"])
                    self.assertEqual(":danger-full-access" if mode == "bypass" else ":workspace", codex["default_permissions"])
                    app = json.loads(app_state.read_text(encoding="utf-8"))
                    self.assertEqual({"remote": "read-only"}, app["electron-persisted-atom-state"]["agent-mode-by-host-id"])

    def test_open_app_blocks_all_writes_and_closed_app_state_is_read_again(self):
        context = self.context()
        app = context.codex_home / ".codex-global-state.json"
        app.parent.mkdir(parents=True, exist_ok=True)
        state = {"electron-persisted-atom-state": {"agent-mode-by-host-id": {"local": "guardian-approvals"}}}
        app.write_text(json.dumps(state), encoding="utf-8")
        before = snapshot(self.root)
        with mock.patch.object(install.host_permissions, "desktop_running", return_value=True):
            with self.assertRaisesRegex(install.InstallError, "Close Codex Desktop"):
                install.apply_native_permissions(context)
        self.assertEqual(before, snapshot(self.root))

        def closed():
            state["saved-during-shutdown"] = True
            app.write_text(json.dumps(state), encoding="utf-8")
            return False

        with mock.patch.object(install.host_permissions, "desktop_running", side_effect=closed):
            install.apply_native_permissions(context)
        self.assertTrue(json.loads(app.read_text(encoding="utf-8"))["saved-during-shutdown"])


if __name__ == "__main__":
    unittest.main()
