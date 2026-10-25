"""open_claude.py: option validation and argument assembly, with resolve_claude_executable/
sync_claude_standards/launch_tab stubbed but everything else (the directory check, the prompt file check
and open_claude_tab's own orchestration) left real."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / '.agents/cc/utility/open/scripts/open_claude.py'

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
import launcher_test_support  # noqa: E402  (after the sys.path fix-up above)

SPEC = importlib.util.spec_from_file_location('open_claude', SCRIPT)
OPEN_CLAUDE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = OPEN_CLAUDE
SPEC.loader.exec_module(OPEN_CLAUDE)


class OpenClaudeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='open claude ')
        self.addCleanup(temp.cleanup)
        self.directory = temp.name
        self.agent_cli = launcher_test_support.real_agent_cli('agent_cli_for_open_claude_tests')
        patcher = mock.patch.object(OPEN_CLAUDE, '_load_agent_cli', return_value=self.agent_cli)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = OPEN_CLAUDE.main(['--working-directory', self.directory, *args])
        return code, out.getvalue(), err.getvalue()

    def bound_payload(self):
        arguments = self.agent_cli.launch_tab.call_args.kwargs['arguments']
        self.assertEqual(
            arguments[:10],
            [
                '-I', '-B', str(self.agent_cli.startup_binding_script()), '--host', 'claude',
                '--project', os.path.abspath(self.directory), '--target', '/bin/claude', '--',
            ],
        )
        return arguments[10:]

    # --- option validation ---

    def test_resume_and_continue_are_mutually_exclusive(self):
        code, _, err = self.run_main('--resume', 'abc', '--continue')
        self.assertEqual(code, 1)
        self.assertIn('not both', err)
        self.agent_cli.launch_tab.assert_not_called()

    def test_prompt_and_prompt_path_are_mutually_exclusive(self):
        code, _, err = self.run_main('--prompt', 'hi', '--prompt-path', '/tmp/whatever')
        self.assertEqual(code, 1)
        self.assertIn('not both', err)
        self.agent_cli.launch_tab.assert_not_called()

    def test_a_long_inline_prompt_is_rejected(self):
        code, _, err = self.run_main('--prompt', 'x' * 501)
        self.assertEqual(code, 1)
        self.assertIn('--prompt-path', err)
        self.agent_cli.launch_tab.assert_not_called()

    def test_a_prompt_path_that_is_not_a_file_is_rejected(self):
        code, _, err = self.run_main('--prompt-path', self.directory)
        self.assertEqual(code, 1)
        self.assertIn('is not a file', err)
        self.agent_cli.launch_tab.assert_not_called()

    def test_a_missing_working_directory_is_rejected_through_the_real_directory_check(self):
        out, err = io.StringIO(), io.StringIO()
        missing = str(Path(self.directory) / 'missing')
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = OPEN_CLAUDE.main(['--working-directory', missing])
        self.assertEqual(code, 1)
        self.assertIn('not a directory', err.getvalue())
        self.assertIn(os.path.abspath(missing), err.getvalue())
        self.agent_cli.launch_tab.assert_not_called()

    def test_a_missing_directory_is_reported_even_when_the_prompt_path_is_also_invalid(self):
        # H14: the directory is validated first, so its error is never shadowed by a later check.
        out, err = io.StringIO(), io.StringIO()
        argv = ['--working-directory', str(Path(self.directory) / 'missing'),
                '--prompt-path', str(Path(self.directory) / 'missing-prompt.md')]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = OPEN_CLAUDE.main(argv)
        self.assertEqual(code, 1)
        self.assertIn('Working directory is not a directory', err.getvalue())

    def test_a_symlinked_working_directory_reaches_the_tab_as_given(self):
        link = Path(self.directory).parent / (Path(self.directory).name + ' link')
        try:
            link.symlink_to(self.directory)
        except OSError:
            self.skipTest('this filesystem or account does not allow creating a symlink')
        self.addCleanup(link.unlink)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = OPEN_CLAUDE.main(['--working-directory', str(link)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(self.agent_cli.launch_tab.call_args.args[0], link)

    # --- argument assembly ---

    def test_resume_routes_through_the_shared_guard(self):
        recovery = mock.Mock()
        recovery.open_session.return_value = {'cwd': self.directory}
        with mock.patch.object(OPEN_CLAUDE, '_load_recovery', return_value=recovery):
            self.run_main('--resume', 'a2bcd5c4-bf6d-4087-95e3-d7ba7f711875')
        recovery.open_session.assert_called_once()
        self.assertEqual(recovery.open_session.call_args.args[:2], ('claude', 'a2bcd5c4-bf6d-4087-95e3-d7ba7f711875'))
        self.agent_cli.launch_tab.assert_not_called()

    def test_resume_reports_recovery_value_error(self):
        recovery = mock.Mock()
        recovery.open_session.side_effect = ValueError('identity requires reconciliation')
        with mock.patch.object(OPEN_CLAUDE, '_load_recovery', return_value=recovery):
            code, _, _ = self.run_main('--resume', 'a2bcd5c4-bf6d-4087-95e3-d7ba7f711875')
        self.assertEqual(code, 1)

    def test_resume_reports_shared_launch_timeout_as_three(self):
        recovery = mock.Mock()
        recovery.open_session.side_effect = self.agent_cli.LaunchTimeout('unknown launch')
        with mock.patch.object(OPEN_CLAUDE, '_load_recovery', return_value=recovery):
            code, _, _ = self.run_main('--resume', 'a2bcd5c4-bf6d-4087-95e3-d7ba7f711875')
        self.assertEqual(code, 3)

    def test_continue_reaches_launch_tab(self):
        self.run_main('--continue')
        arguments = self.agent_cli.launch_tab.call_args.kwargs['arguments']
        self.assertIn('--continue', arguments)

    def test_model_reaches_launch_tab(self):
        self.run_main('--model', 'claude-sonnet-5')
        arguments = self.agent_cli.launch_tab.call_args.kwargs['arguments']
        self.assertEqual(arguments[arguments.index('--model') + 1], 'claude-sonnet-5')

    def test_dangerously_skip_permissions_reaches_launch_tab(self):
        self.run_main('--dangerously-skip-permissions')
        arguments = self.agent_cli.launch_tab.call_args.kwargs['arguments']
        self.assertIn('--dangerously-skip-permissions', arguments)

    def test_an_inline_prompt_is_passed_through(self):
        self.run_main('--prompt', 'a short instruction')
        arguments = self.agent_cli.launch_tab.call_args.kwargs['arguments']
        self.assertIn('a short instruction', arguments)

    def test_a_prompt_path_becomes_the_read_the_file_sentence(self):
        prompt_path = Path(self.directory) / 'draft.md'
        prompt_path.write_text('do the thing\n')
        self.run_main('--prompt-path', str(prompt_path))
        arguments = self.agent_cli.launch_tab.call_args.kwargs['arguments']
        sentence = next(a for a in arguments if a.startswith('Read the file at'))
        self.assertIn(str(prompt_path.resolve()), sentence)
        self.assertIn('working from the current directory', sentence)

    def test_no_session_or_prompt_flags_means_no_extra_arguments(self):
        self.run_main()
        arguments = self.bound_payload()
        self.assertEqual(arguments, [])

    def test_the_title_reaches_open_claude_tab(self):
        self.run_main('--title', 'My Tab')
        args = self.agent_cli.launch_tab.call_args.args
        self.assertEqual(args[2], 'My Tab')

    # --- forced environment, sync-before-launch (H10: order) ---

    def test_force_environment_matches_the_native_claude_colour_contract(self):
        self.run_main()
        force = self.agent_cli.launch_tab.call_args.kwargs['force']
        self.assertEqual(force, {'FORCE_COLOR': '1', 'TERM': 'xterm-256color'})

    def test_executable_is_resolved_before_bound_session_launch(self):
        parent = mock.Mock()
        parent.attach_mock(self.agent_cli.resolve_claude_executable, 'resolve_claude_executable')
        parent.attach_mock(self.agent_cli.sync_claude_standards, 'sync_claude_standards')
        parent.attach_mock(self.agent_cli.launch_tab, 'launch_tab')
        self.run_main()
        self.assertEqual(
            [call[0] for call in parent.mock_calls],
            ['resolve_claude_executable', 'launch_tab'],
        )
        self.assertEqual(self.bound_payload(), [])

    # --- H6: stdio hardened against an unencodable title or path ---

    def test_stdio_is_hardened_against_encoding_errors(self):
        with mock.patch.object(self.agent_cli, 'make_stdio_encoding_lossy') as hardened:
            self.run_main()
        hardened.assert_called_once()

    def test_stdio_is_hardened_before_open_claude_tab_is_called(self):
        # H27: hardening stdout/stderr against an unencodable title or path must happen before the tab can
        # open, not after -- otherwise the success print for a launch that already happened is exactly
        # what could crash unguarded.
        parent = mock.Mock()
        with mock.patch.object(self.agent_cli, 'make_stdio_encoding_lossy') as hardened, \
                mock.patch.object(self.agent_cli, 'open_claude_tab', wraps=self.agent_cli.open_claude_tab) as tab:
            parent.attach_mock(hardened, 'make_stdio_encoding_lossy')
            parent.attach_mock(tab, 'open_claude_tab')
            self.run_main()
        self.assertEqual(
            [call[0] for call in parent.mock_calls],
            ['make_stdio_encoding_lossy', 'open_claude_tab'],
        )

    # --- success / failure ---

    def test_success_prints_the_launched_message(self):
        code, out, _ = self.run_main('--title', 'Tab Name')
        self.assertEqual(code, 0)
        self.assertIn("Launched claude tab 'Tab Name' in", out)
        self.assertIn(os.path.abspath(self.directory), out)

    def test_a_launch_error_from_launch_tab_is_reported_and_exits_nonzero(self):
        self.agent_cli.launch_tab.side_effect = self.agent_cli.LaunchError('no terminal detected')
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn('no terminal detected', err)
        self.assertNotIn('Launched', out)

    def test_a_launch_error_from_resolving_claude_is_reported_and_exits_nonzero(self):
        self.agent_cli.resolve_claude_executable.side_effect = self.agent_cli.LaunchError('no native claude found')
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn('no native claude found', err)
        self.assertEqual(out, '')
        self.agent_cli.launch_tab.assert_not_called()

    # --- H2: a LaunchTimeout is distinct from a definite failure ---

    def test_a_launch_timeout_from_launch_tab_exits_3_and_tells_the_caller_not_to_retry_blind(self):
        self.agent_cli.launch_tab.side_effect = self.agent_cli.LaunchTimeout(
            'Windows Terminal did not answer within 30 seconds; it may still have taken effect.'
        )
        code, out, err = self.run_main()
        self.assertEqual(code, 3)
        self.assertIn('did not answer within 30 seconds', err)
        self.assertIn('may already have opened', err)
        self.assertNotIn('Launched', out)


class OpenClaudeEndToEndTests(unittest.TestCase):
    """main() through the REAL agent_cli.launch_tab and open_claude_tab (H7) -- only
    resolve_claude_executable and sync_claude_standards are mocked. Exercises the real tmux handler, the
    real environment scrub and the real argument assembly, with only `subprocess.run` faked."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='open claude e2e ')
        self.addCleanup(temp.cleanup)
        self.directory = temp.name

        self.calls = []

        def fake_run(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        self.agent_cli = launcher_test_support.real_agent_cli_for_e2e('agent_cli_for_open_claude_e2e_tests', fake_run)

        windows_patcher = mock.patch.object(self.agent_cli, 'IS_WINDOWS', False)
        windows_patcher.start()
        self.addCleanup(windows_patcher.stop)

        load_patcher = mock.patch.object(OPEN_CLAUDE, '_load_agent_cli', return_value=self.agent_cli)
        load_patcher.start()
        self.addCleanup(load_patcher.stop)

        # Only TMUX is set, so the real detection order picks the tmux handler and nothing else.
        environ_patcher = mock.patch.dict(os.environ, {'TMUX': 'session'}, clear=True)
        environ_patcher.start()
        self.addCleanup(environ_patcher.stop)

    def test_main_reaches_the_real_tmux_handler_with_the_exact_expected_argv(self):
        argv = [
            '--working-directory', self.directory,
            '--title', 'e2e open',
            '--model', 'claude-sonnet-5',
            '--dangerously-skip-permissions',
            '--prompt', 'a short instruction',
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = OPEN_CLAUDE.main(argv)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(len(self.calls), 1)
        cmd, kwargs = self.calls[0]

        self.assertEqual(cmd[:4], ['tmux', 'new-window', '-n', 'e2e open'])
        self.assertEqual(cmd[4], '--')
        inner = cmd[5:]
        directory = str(Path(os.path.abspath(self.directory)))
        self.assertEqual(inner[:5], ['sh', '-c', 'cd "$1" && shift && exec "$@"', 'sh', directory])
        self.assertEqual(inner[5], 'env')

        binding_index = inner.index(sys.executable)
        self.assertEqual(
            inner[binding_index:],
            [
                sys.executable, '-I', '-B', str(self.agent_cli.startup_binding_script()), '--host', 'claude',
                '--project', directory, '--target', '/bin/claude', '--', '--dangerously-skip-permissions',
                '--model', 'claude-sonnet-5', 'a short instruction',
            ],
        )

        env_args = inner[6:binding_index]
        self.assertIn('-u', env_args)
        self.assertIn('CLAUDECODE', env_args)
        self.assertIn('AGENT_CLI_TAB_TITLE=e2e open', env_args)
        self.assertIn('FORCE_COLOR=1', env_args)
        # TERM is never forced or cleared on POSIX: the terminal that starts the tab sets it itself.
        self.assertFalse(any(arg.startswith('TERM=') for arg in env_args))

        self.assertEqual(kwargs.get('stdin'), subprocess.DEVNULL)


class InstalledLayoutTests(unittest.TestCase):
    """Every layout the package ships open_claude.py in must find the shared agent_cli.py beside it."""

    LAYOUTS = ('skills',)

    def test_the_authored_and_every_packaged_copy_loads_agent_cli(self):
        copies = [SCRIPT] + [ROOT / 'plugins/cc' / layout / 'open/scripts/open_claude.py'
                             for layout in self.LAYOUTS]
        for copy in copies:
            with self.subTest(copy=str(copy.relative_to(ROOT))):
                self.assertTrue(copy.is_file(), f'{copy} is missing; run pwsh .agents/sync-generated.ps1 first')
                spec = importlib.util.spec_from_file_location(f'open_claude_{abs(hash(copy))}', copy)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertTrue(callable(module._load_agent_cli().launch_tab))


if __name__ == '__main__':
    unittest.main()
