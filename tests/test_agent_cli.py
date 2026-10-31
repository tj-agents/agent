"""agent_cli.py: synthetic homes, PATHs and plugin registries only; the real profile is never read."""
import contextlib
import importlib.util
import io
import json
import os
import shlex
import shutil
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('agent_cli', ROOT / 'plugins/agent/.agents/agent/runtime/agent_cli.py')
CLI = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CLI
SPEC.loader.exec_module(CLI)


# A model of what Windows Terminal does to `wt --window 0 new-tab ... --suppressApplicationTitle <exe>
# <args>` before the tab process sees it, ported from the former C# launcher fixture, where each stage was
# checked against a real wt.exe. First it splits its own argv on `;` into subcommands, unescaping
# `\;` to a literal `;` by dropping one backslash. Then it re-joins the tab command into one string, quoting
# a token only when it contains a space and escaping nothing inside it, and the tab process parses that
# string by CommandLineToArgvW rules.
def split_subcommands(args):
    commands, current = [], []
    for arg in args:
        token = ''
        for character in arg:
            if character != ';':
                token += character
            elif token.endswith('\\'):
                token = token[:-1] + ';'
            else:
                if token:
                    current.append(token)
                    token = ''
                commands.append(current)
                current = []
        if token:
            current.append(token)
    commands.append(current)
    return commands


def command_line_to_argv(line):
    """CommandLineToArgvW for every argument after argv[0], which this model never reads."""
    args, current, in_quotes, have_token, i = [], '', False, False, 0
    while i < len(line):
        character = line[i]
        if character == '\\':
            run = 0
            while i < len(line) and line[i] == '\\':
                run += 1
                i += 1
            if i < len(line) and line[i] == '"':
                current += '\\' * (run // 2)
                if run % 2:
                    current += '"'
                    i += 1
            else:
                current += '\\' * run
            have_token = True
            continue
        if character == '"':
            in_quotes = not in_quotes
            have_token = True
        elif character in ' \t' and not in_quotes:
            if have_token:
                args.append(current)
            current, have_token = '', False
        else:
            current += character
            have_token = True
        i += 1
    if have_token:
        args.append(current)
    return args[1:]


def deliver(command):
    start = command.index('--suppressApplicationTitle')
    joined = 'stub.exe' + ''.join(' ' + (f'"{token}"' if ' ' in token else token) for token in command[start + 1:])
    return command_line_to_argv(joined)


def through_windows_terminal(executable, *arguments):
    """(subcommand count, argv the tab process receives) for one escaped new-tab invocation."""
    argv = ['--window', '0', 'new-tab', '--startingDirectory', r'C:\work dir', '--title', 'a title',
            '--suppressApplicationTitle', executable, *arguments]
    commands = split_subcommands([CLI.terminal_argument(value) for value in argv])
    return len(commands), deliver(commands[0])


@unittest.skipUnless(CLI.IS_WINDOWS, 'the real CommandLineToArgvW exists only on Windows')
class CommandLineModelTests(unittest.TestCase):
    """Pins the Python model of CommandLineToArgvW to the real API, which the model stands in for elsewhere."""

    def real(self, line):
        import ctypes
        from ctypes import wintypes
        shell32 = ctypes.windll.shell32
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        count = ctypes.c_int()
        argv = shell32.CommandLineToArgvW(line, ctypes.byref(count))
        try:
            return [argv[i] for i in range(1, count.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(argv)

    def test_the_model_matches_the_real_api_for_every_delivered_command_line(self):
        values = ['Phase one: migrate Auth; then Search.\nPhase two: verify "end to end"; report back.',
                  'C:\\dir with space\\', 'next', 'C:\\two\\\\ trailing\\\\', 'a\\"b c', 'a\\\\"b',
                  '"', 'say "hi"', 'x\\;y', 'a b\\;', 'a;b', ';', 'a\\\\;b c', 'tab\there too']
        commands = split_subcommands([CLI.terminal_argument(value) for value in
                                      ['--suppressApplicationTitle', 'C:\\Users\\Test User\\claude.exe', *values]])
        self.assertEqual(len(commands), 1)
        joined = 'stub.exe' + ''.join(' ' + (f'"{token}"' if ' ' in token else token) for token in commands[0][1:])
        self.assertEqual(command_line_to_argv(joined), self.real(joined))
        self.assertEqual(self.real(joined), ['C:\\Users\\Test User\\claude.exe', *values])


class TerminalArgumentTests(unittest.TestCase):
    CLAUDE = r'C:\Users\Test User\.local\bin\claude.exe'

    def assert_delivered(self, *arguments):
        count, delivered = through_windows_terminal(self.CLAUDE, *arguments)
        self.assertEqual(count, 1, 'a semicolon split the terminal command line into several subcommands')
        self.assertEqual(delivered, [self.CLAUDE, *arguments])

    def test_the_handoff_prompt_that_was_truncated_reaches_the_tab_whole(self):
        self.assert_delivered('Phase one: migrate Auth; then Search.\nPhase two: verify "end to end"; report back.')

    def test_a_spaced_executable_path_reaches_the_tab_whole(self):
        self.assert_delivered('--resume', 'a2bcd5c4-bf6d-4087-95e3-d7ba7f711875')

    def test_a_trailing_backslash_in_a_spaced_value_does_not_swallow_the_next_argument(self):
        self.assert_delivered('C:\\dir with space\\', 'next', 'C:\\two\\\\ trailing\\\\')

    def test_backslashes_before_a_quote_survive(self):
        self.assert_delivered('a\\"b c', 'a\\\\"b', '"', 'say "hi"')

    def test_backslashes_before_a_semicolon_survive(self):
        self.assert_delivered('x\\;y', 'a b\\;', 'a;b', ';', 'a\\\\;b c')

    def test_plain_values_pass_through_unchanged(self):
        for value in ('--resume', 'abc', r'C:\plain\path', 'model_reasoning_effort=high'):
            self.assertEqual(CLI.terminal_argument(value), value)

    def test_a_line_break_or_tab_without_a_space_is_refused_rather_than_dropped(self):
        for value in ('a\nb', 'a\r\nb', 'a\tb'):
            with self.assertRaises(CLI.LaunchError):
                CLI.terminal_argument(value)

    def test_an_empty_value_is_refused_rather_than_dropped(self):
        with self.assertRaises(CLI.LaunchError):
            CLI.terminal_argument('')

    def test_a_line_break_or_tab_beside_a_space_is_carried(self):
        self.assert_delivered('a b\nc', 'tab\there too')


class LaunchEnvironmentTests(unittest.TestCase):
    def test_session_state_is_cleared_and_forced_values_win_over_a_clear(self):
        cleared, forced = CLI.launch_environment(clear=('TERM', 'CLAUDECODE'), force={'NO_COLOR': '0', 'X': '1'})
        self.assertIn('CLAUDECODE', cleared)
        self.assertIn('CLAUDE_CODE_MESSAGING_SOCKET', cleared)
        self.assertIn('TERM', cleared)
        self.assertNotIn('NO_COLOR', cleared)
        self.assertEqual(len(cleared), len(set(cleared)))
        self.assertEqual(forced, {'NO_COLOR': '0', 'X': '1'})

    def test_machine_configuration_is_not_session_state(self):
        self.assertNotIn('CLAUDE_CODE_GIT_BASH_PATH', CLI.SESSION_ENVIRONMENT)


def executable(path, body='#!/bin/sh\nexit 0\n'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class ClaudeExecutableTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='agent cli ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.name = 'claude.exe' if CLI.IS_WINDOWS else 'claude'

    def test_the_native_install_under_home_wins(self):
        native = executable(self.base / 'home' / '.local' / 'bin' / self.name, '\x7fELF')
        self.assertEqual(CLI.resolve_claude_executable(home=self.base / 'home', which=lambda _: None), str(native))

    @unittest.skipIf(CLI.IS_WINDOWS, 'the #! shim check is POSIX-only')
    def test_an_npm_shim_on_path_is_refused(self):
        shim = executable(self.base / 'npm' / 'claude', '#!/usr/bin/env node\n')
        with self.assertRaises(CLI.LaunchError):
            CLI.resolve_claude_executable(home=self.base / 'empty', which=lambda _: str(shim))

    @unittest.skipIf(CLI.IS_WINDOWS, 'the #! shim check is POSIX-only')
    def test_an_npm_shim_at_the_native_install_path_is_refused(self):
        executable(self.base / 'npm' / 'cli.js', '#!/usr/bin/env node\n')
        local = self.base / 'home' / '.local' / 'bin' / 'claude'
        local.parent.mkdir(parents=True)
        local.symlink_to(self.base / 'npm' / 'cli.js')
        native = executable(self.base / 'path' / 'claude', '\x7fELF')
        self.assertEqual(CLI.resolve_claude_executable(home=self.base / 'home', which=lambda _: str(native)), str(native))
        with self.assertRaises(CLI.LaunchError):
            CLI.resolve_claude_executable(home=self.base / 'home', which=lambda _: None)

    def test_a_native_binary_on_path_is_accepted(self):
        native = executable(self.base / 'path' / self.name, '\x7fELF')
        self.assertEqual(CLI.resolve_claude_executable(home=self.base / 'empty', which=lambda _: str(native)), str(native))


class CodexVersionTests(unittest.TestCase):
    def order(self, left, right):
        return CLI.compare_codex_version(CLI.parse_codex_version(left), CLI.parse_codex_version(right))

    def test_parses_the_cli_banner(self):
        version = CLI.parse_codex_version('codex-cli 0.160.0\n')
        self.assertEqual(version['core'], (0, 160, 0))
        self.assertEqual(version['text'], '0.160.0')
        self.assertIsNone(CLI.parse_codex_version('no version here'))

    def test_a_prerelease_ranks_below_its_release(self):
        self.assertEqual(self.order('0.151.0-alpha.7.1', '0.151.0'), -1)
        self.assertEqual(self.order('0.151.0', '0.150.9'), 1)
        self.assertEqual(self.order('0.151.0-alpha.10', '0.151.0-alpha.9'), 1)
        self.assertEqual(self.order('0.151.0-alpha', '0.151.0-alpha.1'), -1)
        self.assertEqual(self.order('0.151.0-beta', '0.151.0-alpha.9'), 1)
        self.assertEqual(self.order('1.2.3', '1.2.3'), 0)


@unittest.skipIf(CLI.IS_WINDOWS, 'stub executables here are POSIX shell scripts')
class CodexExecutableTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='agent cli ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)

    def vendored(self, prefix, version):
        package = prefix / 'lib' / 'node_modules' / '@openai' / 'codex'
        executable(package / 'bin' / 'codex.js', '#!/usr/bin/env node\n')
        binary = executable(package / 'node_modules' / '@openai' / 'codex-linux-x64' / 'vendor' / 'x86_64-unknown-linux-musl' / 'bin' / 'codex',
                            f'#!/bin/sh\necho "codex-cli {version}"\n')
        shim = prefix / 'bin' / 'codex'
        shim.parent.mkdir(parents=True)
        shim.symlink_to(Path('..') / 'lib' / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js')
        return shim, binary

    def test_finds_the_vendored_binary_behind_a_posix_npm_symlink(self):
        shim, binary = self.vendored(self.base / 'npm global', '0.160.0')
        self.assertEqual(CLI.codex_candidate_paths(which=lambda _: str(shim)), [str(binary)])

    def test_a_native_executable_directly_on_path_is_a_candidate_with_no_npm_package(self):
        native = executable(self.base / 'standalone' / 'codex', '\x7fELFnot a script\n')
        self.assertEqual(CLI.codex_candidate_paths(which=lambda _: str(native)), [str(native)])

    def test_an_npm_shim_on_path_is_not_itself_a_candidate(self):
        shim, binary = self.vendored(self.base / 'npm global', '0.160.0')
        candidates = CLI.codex_candidate_paths(which=lambda _: str(shim))
        self.assertNotIn(str(shim), candidates)
        self.assertEqual(candidates, [str(binary)])

    def test_skips_the_windows_desktop_cache_elsewhere(self):
        executable(self.base / 'appdata' / 'OpenAI' / 'Codex' / 'bin' / 'h' / 'codex.exe')
        self.assertEqual(
            CLI.codex_candidate_paths(which=lambda _: None, local_app_data=str(self.base / 'appdata'), npm_prefix=lambda: None),
            [],
        )

    def test_falls_back_to_the_npm_prefix_when_the_shim_is_off_path(self):
        shim, binary = self.vendored(self.base / 'npm global', '0.160.0')
        self.assertEqual(
            CLI.codex_candidate_paths(which=lambda _: None, npm_prefix=lambda: str(self.base / 'npm global')),
            [str(binary)],
        )

    def test_the_npm_prefix_fallback_is_skipped_once_which_finds_a_shim(self):
        self.vendored(self.base / 'npm global', '0.160.0')
        other_shim, other_binary = self.vendored(self.base / 'other npm', '0.161.0')
        self.assertEqual(
            CLI.codex_candidate_paths(which=lambda _: str(other_shim), npm_prefix=lambda: str(self.base / 'npm global')),
            [str(other_binary)],
        )

    def test_a_missing_npm_prefix_binary_is_not_fatal(self):
        self.assertEqual(
            CLI.codex_candidate_paths(which=lambda _: None, npm_prefix=lambda: str(self.base / 'no such prefix')),
            [],
        )

    @unittest.skipUnless(CLI.IS_WINDOWS, 'the npm-prefix fallback is POSIX-only')
    def test_npm_prefix_is_not_consulted_on_windows(self):
        calls = []
        self.vendored(self.base / 'npm global', '0.160.0')
        CLI.codex_candidate_paths(which=lambda _: None, npm_prefix=lambda: calls.append(1) or str(self.base / 'npm global'))
        self.assertEqual(calls, [])

    def test_picks_the_newest_answering_candidate(self):
        old = executable(self.base / 'old', '#!/bin/sh\necho "codex-cli 0.155.0"\n')
        alpha = executable(self.base / 'alpha', '#!/bin/sh\necho "codex-cli 0.160.0-alpha.3"\n')
        new = executable(self.base / 'new', '#!/bin/sh\necho "codex-cli 0.160.0"\n')
        silent = executable(self.base / 'silent', '#!/bin/sh\nexit 1\n')
        path, version = CLI.resolve_codex_executable(candidates=[str(old), str(silent), str(new), str(alpha)])
        self.assertEqual((path, version['text']), (str(new), '0.160.0'))

    def test_refuses_a_build_below_the_minimum_and_lists_what_was_found(self):
        old = executable(self.base / 'old', '#!/bin/sh\necho "codex-cli 0.150.0"\n')
        with self.assertRaisesRegex(CLI.LaunchError, r'0\.150\.0.*below the required 0\.154\.0(?s:.*)old'):
            CLI.resolve_codex_executable(candidates=[str(old)])

    def test_refuses_when_nothing_answers(self):
        with self.assertRaises(CLI.LaunchError):
            CLI.resolve_codex_executable(candidates=[str(self.base / 'missing')])


class LaneModelTests(unittest.TestCase):
    def table(self, harness):
        return json.loads((ROOT / 'plugins/agent/.agents/agent/resources/lanes' / f'{harness}.json').read_text(encoding='utf-8-sig'))

    def test_a_lane_resolves_to_its_tables_model_and_effort(self):
        for harness in ('claude', 'codex'):
            table = self.table(harness)
            effort_key = table.get('effort_key', 'effort')
            for lane, rung in table['lanes'].items():
                self.assertEqual(CLI.resolve_lane_model(harness, lane=lane), (rung['model'], rung.get(effort_key)))

    def test_the_frontier_tier_resolves_from_its_own_entry(self):
        for harness in ('claude', 'codex'):
            table = self.table(harness)
            if 'frontier' in table:
                self.assertEqual(CLI.resolve_lane_model(harness, frontier=True)[0], table['frontier']['model'])

    def test_lane_and_frontier_are_exclusive_and_one_is_required(self):
        for kwargs in ({}, {'lane': 'L1', 'frontier': True}):
            with self.assertRaises(CLI.LaunchError):
                CLI.resolve_lane_model('claude', **kwargs)

    def test_an_unknown_lane_is_an_error_not_a_fallback(self):
        with self.assertRaisesRegex(CLI.LaunchError, 'unknown lane'):
            CLI.resolve_lane_model('codex', lane='L9')


class PromptFileArgumentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='agent cli prompt ')
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def test_a_prepared_prompt_file_becomes_the_read_the_file_sentence_and_its_resolved_path(self):
        prompt = self.directory / 'draft prompt.md'
        prompt.write_text('do the thing\n')
        sentence, resolved = CLI.prompt_file_argument(str(prompt))
        self.assertEqual(resolved, prompt.resolve())
        self.assertEqual(
            sentence,
            f'Read the file at {prompt.resolve()} and follow its instructions, working from the current directory.',
        )

    def test_a_prompt_path_that_is_not_a_file_is_a_launch_error(self):
        with self.assertRaisesRegex(CLI.LaunchError, 'is not a file'):
            CLI.prompt_file_argument(str(self.directory))


class ResolveTabDirectoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='agent cli tab dir ')
        self.addCleanup(temp.cleanup)
        self.directory = temp.name

    def test_an_existing_directory_is_returned_absolute_but_not_resolved(self):
        previous = os.getcwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, previous)
        self.assertEqual(CLI.resolve_tab_directory('.'), Path(os.path.abspath('.')))

    def test_a_missing_directory_is_a_launch_error_naming_the_absolute_path(self):
        missing = os.path.join(self.directory, 'missing')
        with self.assertRaisesRegex(CLI.LaunchError, 'not a directory'):
            CLI.resolve_tab_directory(missing)
        try:
            CLI.resolve_tab_directory(missing)
        except CLI.LaunchError as exc:
            self.assertIn(os.path.abspath(missing), str(exc))

    def test_a_symlinked_directory_is_kept_as_given(self):
        link = Path(self.directory).parent / (Path(self.directory).name + ' link')
        try:
            link.symlink_to(self.directory)
        except OSError:
            self.skipTest('this filesystem or account does not allow creating a symlink')
        self.addCleanup(link.unlink)
        self.assertEqual(CLI.resolve_tab_directory(str(link)), link)


class ReportLaunchFailureTests(unittest.TestCase):
    def test_a_plain_launch_error_prints_once_and_returns_1(self):
        err = io.StringIO()
        code = CLI.report_launch_failure(CLI.LaunchError('no terminal detected'), err=err)
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue().strip(), 'no terminal detected')

    def test_a_launch_timeout_prints_guidance_and_returns_3(self):
        err = io.StringIO()
        code = CLI.report_launch_failure(
            CLI.LaunchTimeout('Windows Terminal did not answer within 30 seconds; it may still have taken effect.'),
            err=err,
        )
        self.assertEqual(code, 3)
        output = err.getvalue()
        self.assertIn('did not answer within 30 seconds', output)
        self.assertIn('may already have opened', output)

    def test_the_default_err_follows_a_sys_stderr_redirected_after_this_module_loaded(self):
        # A default parameter bound directly to sys.stderr would capture the stream object that existed
        # when agent_cli.py was first loaded, not whatever sys.stderr is redirected to later -- exactly
        # the mistake that silently swallowed every launcher error message under test until it was caught.
        captured = io.StringIO()
        with mock.patch.object(CLI.sys, 'stderr', captured):
            code = CLI.report_launch_failure(CLI.LaunchError('no terminal detected'))
        self.assertEqual(code, 1)
        self.assertIn('no terminal detected', captured.getvalue())


class StdioEncodingTests(unittest.TestCase):
    """make_stdio_encoding_lossy: H6 -- a title or path that a console code page cannot encode must not
    crash the success print for a launch that already happened."""

    def test_every_stream_with_reconfigure_is_switched_to_replace_errors(self):
        stream = mock.Mock(spec=['reconfigure'])
        with mock.patch.object(CLI.sys, 'stdout', stream), mock.patch.object(CLI.sys, 'stderr', stream):
            CLI.make_stdio_encoding_lossy()
        self.assertEqual(stream.reconfigure.call_count, 2)
        stream.reconfigure.assert_called_with(errors='replace')

    def test_a_stream_without_reconfigure_is_left_alone_rather_than_raising(self):
        stream = object()
        with mock.patch.object(CLI.sys, 'stdout', stream), mock.patch.object(CLI.sys, 'stderr', stream):
            CLI.make_stdio_encoding_lossy()  # must not raise AttributeError


def fake_run(results=()):
    """A `run` stand-in returning each (stdout, stderr, returncode) in turn and recording every call."""
    queue = list(results)
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        stdout, stderr, returncode = queue.pop(0) if queue else ('', '', 0)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    run.calls = calls
    return run


class LaunchTabTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='agent cli tab ')
        self.addCleanup(temp.cleanup)
        self.directory = str(Path(temp.name).resolve())
        # The POSIX handlers run only on POSIX, so these tests run as POSIX on every platform; the Windows
        # Terminal tests patch it back to Windows themselves.
        patcher = mock.patch.object(CLI, 'IS_WINDOWS', False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_missing_working_directory_is_rejected_before_any_handler_runs(self):
        with self.assertRaisesRegex(CLI.LaunchError, 'not a directory'):
            CLI.launch_tab(os.path.join(self.directory, 'missing'), '/bin/exe', 'Tab',
                            environ={'TMUX': 'x'}, run=fake_run(), popen=mock.Mock())

    def test_a_relative_working_directory_is_resolved_before_it_reaches_the_tab(self):
        run = fake_run([('', '', 0)])
        previous = os.getcwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, previous)
        CLI.launch_tab('.', '/bin/exe', 'Tab', environ={'TMUX': 'x'}, run=run, popen=mock.Mock())
        self.assertIn(self.directory, run.calls[0][0])
        self.assertNotIn('.', run.calls[0][0])

    def test_detection_order_tmux_wins_over_kitty_window_id(self):
        run = fake_run([('', '', 0)])
        CLI.launch_tab(self.directory, '/bin/exe', 'Tab',
                        environ={'TMUX': 'session', 'KITTY_WINDOW_ID': '5', 'KITTY_LISTEN_ON': 'unix:/x'},
                        run=run, popen=mock.Mock())
        self.assertEqual(run.calls[0][0][0], 'tmux')

    def test_detection_order_kitty_wins_over_konsole(self):
        run = fake_run([('', '', 0)])
        CLI.launch_tab(self.directory, '/bin/exe', 'Tab',
                        environ={'KITTY_WINDOW_ID': '5', 'KITTY_LISTEN_ON': 'unix:/x',
                                 'KONSOLE_DBUS_WINDOW': '/Windows/1'},
                        run=run, popen=mock.Mock())
        self.assertEqual(run.calls[0][0][0], 'kitty')

    def test_tmux_handler_exact_argv(self):
        run = fake_run([('', '', 0)])
        CLI.launch_tab(self.directory, '/bin/exe', 'My Title', arguments=['--flag', 'v'],
                        environ={'TMUX': 'session'}, run=run, popen=mock.Mock())
        cmd, kwargs = run.calls[0]
        self.assertEqual(cmd[:8], ['tmux', 'new-window', '-n', 'My Title', '-P', '-F', '#{pane_id}', '--'])
        inner = cmd[8:]
        self.assertEqual(inner[:5], ['sh', '-c', 'cd "$1" && shift && exec "$@"', 'sh', self.directory])
        self.assertEqual(inner[5], 'env')
        self.assertIn('AGENT_CLI_TAB_TITLE=My Title', inner)
        self.assertEqual(inner[-3:], ['/bin/exe', '--flag', 'v'])
        self.assertEqual(kwargs, {'stdin': subprocess.DEVNULL, 'capture_output': True, 'text': True, 'timeout': 30})

    def test_tmux_titles_the_new_pane_so_inventory_reads_the_launch_title(self):
        run = fake_run([('%7\n', '', 0)])
        CLI.launch_tab(self.directory, '/bin/exe', 'My Title', environ={'TMUX': 'session'}, run=run, popen=mock.Mock())
        self.assertEqual(run.calls[1][0], ['tmux', 'select-pane', '-t', '%7', '-T', 'My Title'])

    def test_kitty_handler_exact_argv(self):
        run = fake_run([('', '', 0)])
        CLI.launch_tab(self.directory, '/bin/exe', 'A Tab',
                        environ={'KITTY_WINDOW_ID': '42', 'KITTY_LISTEN_ON': 'unix:/tmp/kitty-99'},
                        run=run, popen=mock.Mock())
        cmd, kwargs = run.calls[0]
        self.assertEqual(cmd[:4], ['kitty', '@', '--to', 'unix:/tmp/kitty-99'])
        self.assertEqual(cmd[4:8], ['launch', '--type=tab', '--match', 'window_id:42'])
        self.assertEqual(cmd[8:12], ['--tab-title', 'A Tab', '--title', 'A Tab'])
        self.assertEqual(cmd[12], 'sh')
        self.assertEqual(kwargs, {'stdin': subprocess.DEVNULL, 'capture_output': True, 'text': True, 'timeout': 30})

    def test_kitty_without_listen_on_raises_and_opens_nothing(self):
        run = fake_run()
        popen = mock.Mock()
        with self.assertRaisesRegex(CLI.LaunchError, 'allow_remote_control socket-only'):
            CLI.launch_tab(self.directory, '/bin/exe', 'Tab', environ={'KITTY_WINDOW_ID': '42'}, run=run, popen=popen)
        self.assertEqual(run.calls, [])
        popen.assert_not_called()

    def test_konsole_handler_exact_dbus_calls_and_script_invocation(self):
        environ = {'KONSOLE_DBUS_WINDOW': '/Windows/1', 'KONSOLE_DBUS_SERVICE': 'org.kde.konsole-123'}
        run = fake_run([('7', '', 0), ('', '', 0), ('', '', 0), ('', '', 0)])
        with mock.patch.object(CLI.shutil, 'which', side_effect=lambda name: '/usr/bin/qdbus6' if name == 'qdbus6' else None):
            CLI.launch_tab(self.directory, '/bin/exe', 'A Tab', environ=environ, run=run, popen=mock.Mock())
        self.assertEqual(run.calls[0][0], ['/usr/bin/qdbus6', 'org.kde.konsole-123', '/Windows/1', 'newSession'])
        for call, context in zip(run.calls[1:3], ('0', '1')):
            self.assertEqual(call[0], ['/usr/bin/qdbus6', 'org.kde.konsole-123', '/Sessions/7',
                                       'setTabTitleFormat', context, 'A Tab'])
        run_command = run.calls[3][0]
        self.assertEqual(run_command[:4], ['/usr/bin/qdbus6', 'org.kde.konsole-123', '/Sessions/7', 'runCommand'])
        self.assertTrue(run_command[4].startswith('exec sh '))
        script = Path(shlex.split(run_command[4])[2])
        self.addCleanup(shutil.rmtree, script.parent, True)
        self.assertTrue(script.is_file())

    def test_a_timed_out_terminal_command_is_a_launch_error(self):
        def run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 30)
        with self.assertRaisesRegex(CLI.LaunchError, 'did not answer within 30 seconds'):
            CLI.launch_tab(self.directory, '/bin/exe', 'Tab', environ={'TMUX': 'x'}, run=run, popen=mock.Mock())

    def test_a_missing_terminal_command_is_a_launch_error(self):
        def run(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])
        with self.assertRaisesRegex(CLI.LaunchError, 'could not be run'):
            CLI.launch_tab(self.directory, '/bin/exe', 'Tab', environ={'TMUX': 'x'}, run=run, popen=mock.Mock())

    def test_a_symlinked_working_directory_is_kept_as_given(self):
        link = Path(self.directory).parent / (Path(self.directory).name + ' link')
        try:
            link.symlink_to(self.directory)
        except OSError:
            self.skipTest('this filesystem or account does not allow creating a symlink')
        self.addCleanup(link.unlink)
        run = fake_run([('', '', 0)])
        CLI.launch_tab(str(link), '/bin/exe', 'Tab', environ={'TMUX': 'x'}, run=run, popen=mock.Mock())
        self.assertIn(str(link), run.calls[0][0])

    def test_a_konsole_title_with_a_percent_sign_is_refused_before_any_tab_opens(self):
        environ = {'KONSOLE_DBUS_WINDOW': '/Windows/1', 'KONSOLE_DBUS_SERVICE': 'org.kde.konsole-123'}
        run = fake_run()
        with mock.patch.object(CLI.shutil, 'which', return_value='/usr/bin/qdbus6'):
            with self.assertRaisesRegex(CLI.LaunchError, 'expand the %'):
                CLI.launch_tab(self.directory, '/bin/exe', 'Fix 100%d', environ=environ, run=run, popen=mock.Mock())
        self.assertEqual(run.calls, [])

    def test_posix_terminal_variables_are_ignored_on_windows(self):
        run = fake_run([('', '', 0)])
        with mock.patch.object(CLI, 'IS_WINDOWS', True), mock.patch.object(CLI.shutil, 'which', return_value='C:/wt.exe'):
            CLI.launch_tab(self.directory, 'C:/claude.exe', 'Tab', environ={'TMUX': 'x', 'WT_SESSION': 'abc'},
                           run=run, popen=mock.Mock())
        self.assertEqual(run.calls[0][0][0], 'C:/wt.exe')

    def test_a_timed_out_konsole_run_command_leaves_a_possibly_running_launch_alone(self):
        environ = {'KONSOLE_DBUS_WINDOW': '/Windows/1', 'KONSOLE_DBUS_SERVICE': 'org.kde.konsole-123'}
        results = iter([('7', '', 0), ('', '', 0), ('', '', 0)])
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[3] == 'runCommand':
                raise subprocess.TimeoutExpired(cmd, 30)
            stdout, stderr, code = next(results)
            return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr=stderr)

        with mock.patch.object(CLI.shutil, 'which', side_effect=lambda name: '/usr/bin/qdbus6' if name == 'qdbus6' else None):
            with self.assertRaises(CLI.LaunchTimeout):
                CLI.launch_tab(self.directory, '/bin/exe', 'A Tab', environ=environ, run=run, popen=mock.Mock())
        script = Path(shlex.split(calls[-1][4])[2])
        self.addCleanup(shutil.rmtree, script.parent, True)
        self.assertTrue(script.is_file(), 'a launch that may be running lost its script')
        self.assertNotIn('sendText', [cmd[3] for cmd in calls])

    def test_a_timed_out_konsole_title_call_still_closes_the_empty_tab(self):
        # H12/H23: no inner command was ever typed into this session, so once abandon() has closed it,
        # the uncertainty a LaunchTimeout names no longer applies -- this is a plain, definite LaunchError,
        # never "may still have taken effect", and it reports that closing the tab itself succeeded.
        environ = {'KONSOLE_DBUS_WINDOW': '/Windows/1', 'KONSOLE_DBUS_SERVICE': 'org.kde.konsole-123'}
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[3] == 'setTabTitleFormat':
                raise subprocess.TimeoutExpired(cmd, 30)
            return subprocess.CompletedProcess(cmd, 0, stdout='7', stderr='')

        with mock.patch.object(CLI.shutil, 'which', side_effect=lambda name: '/usr/bin/qdbus6' if name == 'qdbus6' else None):
            with self.assertRaises(CLI.LaunchError) as caught:
                CLI.launch_tab(self.directory, '/bin/exe', 'A Tab', environ=environ, run=run, popen=mock.Mock())
        self.assertNotIsInstance(caught.exception, CLI.LaunchTimeout)
        message = str(caught.exception)
        self.assertNotIn('may still have taken effect', message)
        self.assertIn('the launch failed', message)
        self.assertNotIn('also failed', message)
        self.assertEqual(calls[-1], ['/usr/bin/qdbus6', 'org.kde.konsole-123', '/Sessions/7', 'sendText', 'exit\n'])
        self.assertNotIn('runCommand', [cmd[3] for cmd in calls])

    def test_a_timed_out_konsole_title_call_when_closing_also_fails_says_so(self):
        environ = {'KONSOLE_DBUS_WINDOW': '/Windows/1', 'KONSOLE_DBUS_SERVICE': 'org.kde.konsole-123'}
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[3] == 'setTabTitleFormat':
                raise subprocess.TimeoutExpired(cmd, 30)
            if cmd[3] == 'sendText':
                return subprocess.CompletedProcess(cmd, 1, stdout='', stderr='no such session')
            return subprocess.CompletedProcess(cmd, 0, stdout='7', stderr='')

        with mock.patch.object(CLI.shutil, 'which', side_effect=lambda name: '/usr/bin/qdbus6' if name == 'qdbus6' else None):
            with self.assertRaises(CLI.LaunchError) as caught:
                CLI.launch_tab(self.directory, '/bin/exe', 'A Tab', environ=environ, run=run, popen=mock.Mock())
        self.assertNotIsInstance(caught.exception, CLI.LaunchTimeout)
        message = str(caught.exception)
        self.assertNotIn('may still have taken effect', message)
        self.assertIn('also failed', message)

    def test_a_timed_out_konsole_new_session_call_is_a_plain_error_naming_a_possible_empty_tab(self):
        # H12: no session id came back, so there is nothing here to close by typing into it -- still a
        # plain LaunchError, not a LaunchTimeout, because no inner command was ever at risk of being
        # mid-flight.
        environ = {'KONSOLE_DBUS_WINDOW': '/Windows/1', 'KONSOLE_DBUS_SERVICE': 'org.kde.konsole-123'}
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[3] == 'newSession':
                raise subprocess.TimeoutExpired(cmd, 30)
            return subprocess.CompletedProcess(cmd, 0, stdout='7', stderr='')

        with mock.patch.object(CLI.shutil, 'which', side_effect=lambda name: '/usr/bin/qdbus6' if name == 'qdbus6' else None):
            with self.assertRaises(CLI.LaunchError) as caught:
                CLI.launch_tab(self.directory, '/bin/exe', 'A Tab', environ=environ, run=run, popen=mock.Mock())
        self.assertNotIsInstance(caught.exception, CLI.LaunchTimeout)
        self.assertIn('empty Konsole tab may have been left open', str(caught.exception))
        self.assertEqual(len(calls), 1, 'nothing can be cleaned up without a session id to address it to')

    def test_a_failed_konsole_run_command_removes_its_script(self):
        environ = {'KONSOLE_DBUS_WINDOW': '/Windows/1', 'KONSOLE_DBUS_SERVICE': 'org.kde.konsole-123'}
        run = fake_run([('7', '', 0), ('', '', 0), ('', '', 0), ('', 'no such session', 1)])
        created = []
        real_write = CLI._write_posix_script
        with mock.patch.object(CLI, '_write_posix_script', side_effect=lambda inner: created.append(real_write(inner)) or created[-1]):
            with mock.patch.object(CLI.shutil, 'which', side_effect=lambda name: '/usr/bin/qdbus6' if name == 'qdbus6' else None):
                with self.assertRaisesRegex(CLI.LaunchError, 'no such session'):
                    CLI.launch_tab(self.directory, '/bin/exe', 'A Tab', environ=environ, run=run, popen=mock.Mock())
        self.assertFalse(created[0].parent.exists())
        # The empty tab it created is closed by ending its shell.
        self.assertEqual(run.calls[-1][0], ['/usr/bin/qdbus6', 'org.kde.konsole-123', '/Sessions/7', 'sendText', 'exit\n'])

    def test_konsole_falls_back_to_qdbus_when_qdbus6_is_absent(self):
        environ = {'KONSOLE_DBUS_WINDOW': '/Windows/1', 'KONSOLE_DBUS_SERVICE': 'org.kde.konsole-123'}
        run = fake_run([('7', '', 0), ('', '', 0), ('', '', 0), ('', '', 0)])
        with mock.patch.object(CLI.shutil, 'which', side_effect=lambda name: '/usr/bin/qdbus' if name == 'qdbus' else None):
            CLI.launch_tab(self.directory, '/bin/exe', 'A Tab', environ=environ, run=run, popen=mock.Mock())
        self.assertEqual(run.calls[0][0][0], '/usr/bin/qdbus')

    def test_konsole_without_qdbus_on_path_raises(self):
        environ = {'KONSOLE_DBUS_WINDOW': '/Windows/1', 'KONSOLE_DBUS_SERVICE': 'org.kde.konsole-123'}
        with mock.patch.object(CLI.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(CLI.LaunchError, 'qdbus'):
                CLI.launch_tab(self.directory, '/bin/exe', 'Tab', environ=environ, run=fake_run(), popen=mock.Mock())

    def test_a_failing_handler_raises_and_never_opens_a_window(self):
        run = fake_run([('', 'tmux: no server running', 1)])
        popen = mock.Mock()
        with self.assertRaisesRegex(CLI.LaunchError, 'no server running'):
            CLI.launch_tab(self.directory, '/bin/exe', 'Tab', environ={'TMUX': 'x'}, run=run, popen=popen)
        popen.assert_not_called()

    def test_term_is_never_forced_or_cleared_on_posix(self):
        run = fake_run([('', '', 0)])
        CLI.launch_tab(self.directory, '/bin/exe', 'Tab', clear=['TERM'],
                        force={'TERM': 'xterm-256color', 'FORCE_COLOR': '1'},
                        environ={'TMUX': 'x'}, run=run, popen=mock.Mock())
        inner = run.calls[0][0]
        self.assertNotIn('TERM', ' '.join(inner))
        self.assertIn('FORCE_COLOR=1', inner)

    def test_windows_terminal_handler_argv_escaped_and_env_passed_via_kwarg(self):
        run = fake_run([('', '', 0)])
        environ = {'WT_SESSION': 'abc', 'SOME_OTHER': 'x', 'CLAUDECODE': '1'}
        with mock.patch.object(CLI, 'IS_WINDOWS', True), mock.patch.object(CLI.shutil, 'which', return_value='/mnt/c/wt.exe'):
            CLI.launch_tab(self.directory, 'C:\\exe with space\\claude.exe', 'A Title',
                            arguments=['--resume', 'id; two'], force={'FORCE_COLOR': '1'},
                            environ=environ, run=run, popen=mock.Mock())
        cmd, kwargs = run.calls[0]
        self.assertEqual(cmd[0], '/mnt/c/wt.exe')
        self.assertEqual(cmd[1:7], ['--window', '0', 'new-tab', '--startingDirectory', self.directory, '--title'])
        self.assertEqual(cmd[7:10], [CLI.terminal_argument('A Title'), '--useApplicationTitle',
                                     CLI.terminal_argument('C:\\exe with space\\claude.exe')])
        self.assertIn(CLI.terminal_argument('id; two'), cmd)
        env = kwargs['env']
        self.assertEqual(env.get('FORCE_COLOR'), '1')
        self.assertNotIn('CLAUDECODE', env)
        # The caller's own environ is read, never mutated: the changes travel only in this call's env=.
        self.assertEqual(environ, {'WT_SESSION': 'abc', 'SOME_OTHER': 'x', 'CLAUDECODE': '1'})
        self.assertEqual(kwargs.get('stdin'), subprocess.DEVNULL)

    def test_windows_terminal_handler_failure_raises(self):
        run = fake_run([('', 'wt.exe: boom', 1)])
        with mock.patch.object(CLI, 'IS_WINDOWS', True), mock.patch.object(CLI.shutil, 'which', return_value='/mnt/c/wt.exe'):
            with self.assertRaisesRegex(CLI.LaunchError, 'boom'):
                CLI.launch_tab(self.directory, '/bin/exe', 'Tab', environ={'WT_SESSION': 'abc'}, run=run, popen=mock.Mock())

    def test_wt_session_inherited_into_wsl_is_not_treated_as_a_terminal(self):
        run = fake_run([('', '', 0)])
        with mock.patch.object(CLI.shutil, 'which', return_value='/mnt/c/wt.exe'):
            CLI.launch_tab(self.directory, '/bin/exe', 'Tab', environ={'WT_SESSION': 'abc', 'TMUX': 'x'},
                           run=run, popen=mock.Mock())
            with self.assertRaisesRegex(CLI.LaunchError, 'no graphical display'):
                CLI.launch_tab(self.directory, '/bin/exe', 'Tab', environ={'WT_SESSION': 'abc'},
                               run=fake_run(), popen=mock.Mock())
        self.assertEqual(run.calls[0][0][0], 'tmux')

    def test_no_terminal_on_windows_with_wt_opens_a_tab_without_a_window_warning(self):
        run = fake_run([('', '', 0)])
        with mock.patch.object(CLI, 'IS_WINDOWS', True), mock.patch.object(CLI.shutil, 'which', return_value='C:/wt.exe'):
            with contextlib.redirect_stderr(io.StringIO()) as captured:
                CLI.launch_tab(self.directory, 'C:/claude.exe', 'Tab', environ={}, run=run, popen=mock.Mock())
        self.assertEqual(run.calls[0][0][1:4], ['--window', '0', 'new-tab'])
        self.assertEqual(captured.getvalue(), '')

    def test_no_terminal_and_no_display_raises_rather_than_reporting_a_launch(self):
        popen = mock.Mock()
        with mock.patch.object(CLI.shutil, 'which', return_value='/usr/bin/xterm'):
            with self.assertRaisesRegex(CLI.LaunchError, 'no graphical display'):
                CLI.launch_tab(self.directory, '/bin/exe', 'Tab', environ={}, run=fake_run(), popen=popen)
        popen.assert_not_called()

    def test_no_terminal_detected_posix_fallback_uses_popen_with_detached_stdio_and_warns(self):
        popen = mock.Mock()
        with mock.patch.object(CLI.shutil, 'which', side_effect=lambda name: '/usr/bin/xterm' if name == 'xterm' else None):
            with contextlib.redirect_stderr(io.StringIO()) as captured:
                CLI.launch_tab(self.directory, '/bin/exe', 'Tab', environ={'WAYLAND_DISPLAY': 'wayland-0'},
                               run=fake_run(), popen=popen)
        self.assertIn('no terminal', captured.getvalue())
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0][0], 'xterm')
        self.assertTrue(kwargs.get('start_new_session'))
        self.assertEqual(kwargs.get('stdin'), subprocess.DEVNULL)
        self.assertEqual(kwargs.get('stdout'), subprocess.DEVNULL)
        self.assertEqual(kwargs.get('stderr'), subprocess.DEVNULL)

    def test_no_terminal_detected_posix_with_nothing_available_raises(self):
        with mock.patch.object(CLI.shutil, 'which', return_value=None):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(CLI.LaunchError, 'No terminal'):
                    CLI.launch_tab(self.directory, '/bin/exe', 'Tab', environ={'DISPLAY': ':0'},
                                   run=fake_run(), popen=mock.Mock())


class OpenClaudeTabBindingTests(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='agent cli open tab ')
        self.addCleanup(temp.cleanup)
        self.directory = temp.name

        self.resolve_claude_executable = mock.Mock(return_value='/bin/claude')
        self.launch_tab = mock.Mock()
        for name, mocked in (
            ('resolve_claude_executable', self.resolve_claude_executable),
            ('launch_tab', self.launch_tab),
        ):
            patcher = mock.patch.object(CLI, name, mocked)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.parent = mock.Mock()
        self.parent.attach_mock(self.resolve_claude_executable, 'resolve_claude_executable')
        self.parent.attach_mock(self.launch_tab, 'launch_tab')

    def test_binds_a_session_child_without_revalidating_the_directory(self):
        missing = os.path.join(self.directory, 'missing')
        CLI.open_claude_tab(missing, 'Tab', [])
        self.resolve_claude_executable.assert_called_once()
        self.launch_tab.assert_called_once_with(
            missing, sys.executable, 'Tab',
            arguments=CLI.bound_terminal_arguments('claude', missing, '/bin/claude', []),
            clear=(), force={'FORCE_COLOR': '1', 'TERM': 'xterm-256color', 'CLAUDE_CODE_DISABLE_TERMINAL_TITLE': '1'},
        )

    def test_resolves_and_binds_the_typed_target_in_order(self):
        result = CLI.open_claude_tab(self.directory, 'Tab', ['--flag'])

        self.assertIsNone(result)
        self.assertEqual(
            [call[0] for call in self.parent.mock_calls],
            ['resolve_claude_executable', 'launch_tab'],
        )
        self.launch_tab.assert_called_once_with(
            self.directory, sys.executable, 'Tab',
            arguments=CLI.bound_terminal_arguments('claude', self.directory, '/bin/claude', ['--flag']),
            clear=(), force={'FORCE_COLOR': '1', 'TERM': 'xterm-256color', 'CLAUDE_CODE_DISABLE_TERMINAL_TITLE': '1'},
        )

    def test_forwards_profile_environment_to_the_tab(self):
        CLI.open_claude_tab(self.directory, 'Tab', [], clear=('CLAUDE_CONFIG_DIR',), force={'EXTRA': '1'})

        self.launch_tab.assert_called_once_with(
            self.directory, sys.executable, 'Tab',
            arguments=CLI.bound_terminal_arguments('claude', self.directory, '/bin/claude', []),
            clear=('CLAUDE_CONFIG_DIR',), force={'FORCE_COLOR': '1', 'TERM': 'xterm-256color', 'CLAUDE_CODE_DISABLE_TERMINAL_TITLE': '1', 'EXTRA': '1'},
        )


@unittest.skipIf(CLI.IS_WINDOWS, 'the POSIX inner command is only ever started on POSIX')
class PosixInnerCommandExecutionTests(unittest.TestCase):
    """Actually runs the POSIX inner command (and the Konsole script that wraps it) through `sh`."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='agent cli exec ')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.directory = self.base / 'work dir with spaces'
        self.directory.mkdir()
        self.recorder = executable(self.base / 'record.py', '#!/usr/bin/env python3\n'
            'import json, os, sys\n'
            'with open(sys.argv[1], "w") as f:\n'
            '    json.dump({"cwd": os.getcwd(), "argv": sys.argv[2:],\n'
            '               "env": {k: os.environ.get(k) for k in ("CLAUDECODE", "NO_COLOR",'
            ' "AGENT_CLI_TAB_TITLE", "FORCE_COLOR")}}, f)\n')
        self.output = self.base / 'result.json'
        self.prompt = 'Phase one: migrate "Auth"; then $Search.\nPhase two: a\\\\b\\;c; done.'
        self.cleared = ['CLAUDECODE', 'NO_COLOR']
        self.forced = {'AGENT_CLI_TAB_TITLE': 'A Title', 'FORCE_COLOR': '1'}
        self.env = dict(os.environ)
        self.env['CLAUDECODE'] = '1'
        self.env['NO_COLOR'] = '1'

    def run_and_check(self, argv):
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.output.read_text())
        self.assertEqual(data['cwd'], str(self.directory))
        self.assertEqual(data['argv'], ['--resume', self.prompt])
        self.assertIsNone(data['env']['CLAUDECODE'])
        self.assertIsNone(data['env']['NO_COLOR'])
        self.assertEqual(data['env']['AGENT_CLI_TAB_TITLE'], 'A Title')
        self.assertEqual(data['env']['FORCE_COLOR'], '1')

    def test_the_inner_command_runs_in_the_directory_with_the_scrubbed_environment_and_whole_argv(self):
        inner = CLI.posix_inner_command(self.directory, self.cleared, self.forced, str(self.recorder),
                                         [str(self.output), '--resume', self.prompt])
        self.run_and_check(inner)

    def test_the_konsole_script_runs_identically_and_deletes_itself(self):
        inner = CLI.posix_inner_command(self.directory, self.cleared, self.forced, str(self.recorder),
                                         [str(self.output), '--resume', self.prompt])
        script_path = CLI._write_posix_script(inner)
        self.assertTrue(script_path.is_file())
        self.run_and_check(['sh', str(script_path)])
        self.assertFalse(script_path.parent.exists(), 'the Konsole script did not delete itself and its directory')


if __name__ == '__main__':
    unittest.main()
