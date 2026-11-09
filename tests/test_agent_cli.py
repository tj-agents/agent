"""agent_cli.py: synthetic homes, PATHs and plugin registries only; the real profile is never read."""
import contextlib
import importlib
import inspect
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
sys.path.insert(0, str(ROOT / 'plugins/agent/.agents/agent/runtime'))
CLI = importlib.import_module('agent_cli')
Host = importlib.import_module('tj_agent.host').Host


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
        self.assertNotIn('CLAUDE_CODE_GIT_BASH_PATH', CLI.host_launch(Host.CLAUDE).SESSION_ENVIRONMENT)


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
        self.assertEqual(CLI.host_launch(Host.CLAUDE).resolve_executable(home=self.base / 'home', which=lambda _: None), str(native))

    @unittest.skipIf(CLI.IS_WINDOWS, 'the #! shim check is POSIX-only')
    def test_an_npm_shim_on_path_is_refused(self):
        shim = executable(self.base / 'npm' / 'claude', '#!/usr/bin/env node\n')
        with self.assertRaises(CLI.LaunchError):
            CLI.host_launch(Host.CLAUDE).resolve_executable(home=self.base / 'empty', which=lambda _: str(shim))

    @unittest.skipIf(CLI.IS_WINDOWS, 'the #! shim check is POSIX-only')
    def test_an_npm_shim_at_the_native_install_path_is_refused(self):
        executable(self.base / 'npm' / 'cli.js', '#!/usr/bin/env node\n')
        local = self.base / 'home' / '.local' / 'bin' / 'claude'
        local.parent.mkdir(parents=True)
        local.symlink_to(self.base / 'npm' / 'cli.js')
        native = executable(self.base / 'path' / 'claude', '\x7fELF')
        self.assertEqual(CLI.host_launch(Host.CLAUDE).resolve_executable(home=self.base / 'home', which=lambda _: str(native)), str(native))
        with self.assertRaises(CLI.LaunchError):
            CLI.host_launch(Host.CLAUDE).resolve_executable(home=self.base / 'home', which=lambda _: None)

    def test_a_native_binary_on_path_is_accepted(self):
        native = executable(self.base / 'path' / self.name, '\x7fELF')
        self.assertEqual(CLI.host_launch(Host.CLAUDE).resolve_executable(home=self.base / 'empty', which=lambda _: str(native)), str(native))


class CodexVersionTests(unittest.TestCase):
    def order(self, left, right):
        return CLI.host_launch(Host.CODEX).compare_version(CLI.host_launch(Host.CODEX).parse_version(left), CLI.host_launch(Host.CODEX).parse_version(right))

    def test_parses_the_cli_banner(self):
        version = CLI.host_launch(Host.CODEX).parse_version('codex-cli 0.160.0\n')
        self.assertEqual(version['core'], (0, 160, 0))
        self.assertEqual(version['text'], '0.160.0')
        self.assertIsNone(CLI.host_launch(Host.CODEX).parse_version('no version here'))

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
        self.assertEqual(CLI.host_launch(Host.CODEX).candidate_paths(which=lambda _: str(shim)), [str(binary)])

    def test_a_native_executable_directly_on_path_is_a_candidate_with_no_npm_package(self):
        native = executable(self.base / 'standalone' / 'codex', '\x7fELFnot a script\n')
        self.assertEqual(CLI.host_launch(Host.CODEX).candidate_paths(which=lambda _: str(native)), [str(native)])

    def test_an_npm_shim_on_path_is_not_itself_a_candidate(self):
        shim, binary = self.vendored(self.base / 'npm global', '0.160.0')
        candidates = CLI.host_launch(Host.CODEX).candidate_paths(which=lambda _: str(shim))
        self.assertNotIn(str(shim), candidates)
        self.assertEqual(candidates, [str(binary)])

    def test_skips_the_windows_desktop_cache_elsewhere(self):
        executable(self.base / 'appdata' / 'OpenAI' / 'Codex' / 'bin' / 'h' / 'codex.exe')
        self.assertEqual(
            CLI.host_launch(Host.CODEX).candidate_paths(which=lambda _: None, local_app_data=str(self.base / 'appdata'), npm_prefix=lambda: None),
            [],
        )

    def test_falls_back_to_the_npm_prefix_when_the_shim_is_off_path(self):
        shim, binary = self.vendored(self.base / 'npm global', '0.160.0')
        self.assertEqual(
            CLI.host_launch(Host.CODEX).candidate_paths(which=lambda _: None, npm_prefix=lambda: str(self.base / 'npm global')),
            [str(binary)],
        )

    def test_the_npm_prefix_fallback_is_skipped_once_which_finds_a_shim(self):
        self.vendored(self.base / 'npm global', '0.160.0')
        other_shim, other_binary = self.vendored(self.base / 'other npm', '0.161.0')
        self.assertEqual(
            CLI.host_launch(Host.CODEX).candidate_paths(which=lambda _: str(other_shim), npm_prefix=lambda: str(self.base / 'npm global')),
            [str(other_binary)],
        )

    def test_a_missing_npm_prefix_binary_is_not_fatal(self):
        self.assertEqual(
            CLI.host_launch(Host.CODEX).candidate_paths(which=lambda _: None, npm_prefix=lambda: str(self.base / 'no such prefix')),
            [],
        )

    @unittest.skipUnless(CLI.IS_WINDOWS, 'the npm-prefix fallback is POSIX-only')
    def test_npm_prefix_is_not_consulted_on_windows(self):
        calls = []
        self.vendored(self.base / 'npm global', '0.160.0')
        CLI.host_launch(Host.CODEX).candidate_paths(which=lambda _: None, npm_prefix=lambda: calls.append(1) or str(self.base / 'npm global'))
        self.assertEqual(calls, [])

    def test_picks_the_newest_answering_candidate(self):
        old = executable(self.base / 'old', '#!/bin/sh\necho "codex-cli 0.155.0"\n')
        alpha = executable(self.base / 'alpha', '#!/bin/sh\necho "codex-cli 0.160.0-alpha.3"\n')
        new = executable(self.base / 'new', '#!/bin/sh\necho "codex-cli 0.160.0"\n')
        silent = executable(self.base / 'silent', '#!/bin/sh\nexit 1\n')
        path, version = CLI.host_launch(Host.CODEX).resolve_executable(candidates=[str(old), str(silent), str(new), str(alpha)])
        self.assertEqual((path, version['text']), (str(new), '0.160.0'))

    def test_refuses_a_build_below_the_minimum_and_lists_what_was_found(self):
        old = executable(self.base / 'old', '#!/bin/sh\necho "codex-cli 0.150.0"\n')
        with self.assertRaisesRegex(CLI.LaunchError, r'0\.150\.0.*below the required 0\.154\.0(?s:.*)old'):
            CLI.host_launch(Host.CODEX).resolve_executable(candidates=[str(old)])

    def test_refuses_when_nothing_answers(self):
        with self.assertRaises(CLI.LaunchError):
            CLI.host_launch(Host.CODEX).resolve_executable(candidates=[str(self.base / 'missing')])


class LaneModelTests(unittest.TestCase):
    def table(self, host):
        return json.loads((ROOT / 'plugins/agent/.agents/agent/resources/lanes' / f'{host}.json').read_text(encoding='utf-8-sig'))

    def test_a_lane_resolves_to_its_tables_model_and_effort(self):
        for host in Host:
            table = self.table(host)
            effort_key = table.get('effort_key', 'effort')
            for lane, rung in table['lanes'].items():
                self.assertEqual(CLI.resolve_lane_model(host, lane=lane), (rung['model'], rung.get(effort_key)))

    def test_the_frontier_tier_resolves_from_its_own_entry(self):
        for host in Host:
            table = self.table(host)
            if 'frontier' in table:
                self.assertEqual(CLI.resolve_lane_model(host, frontier=True)[0], table['frontier']['model'])

    def test_lane_and_frontier_are_exclusive_and_one_is_required(self):
        for kwargs in ({}, {'lane': 'L1', 'frontier': True}):
            with self.assertRaises(CLI.LaunchError):
                CLI.resolve_lane_model(Host.CLAUDE, **kwargs)

    def test_an_unknown_lane_is_an_error_not_a_fallback(self):
        with self.assertRaisesRegex(CLI.LaunchError, 'unknown lane'):
            CLI.resolve_lane_model(Host.CODEX, lane='L9')


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


class OpenTabTests(unittest.TestCase):
    def test_windows_terminal_opens_an_escaped_titled_tab_with_the_scrubbed_environment(self):
        run = fake_run()
        with mock.patch.object(CLI, 'IS_WINDOWS', True), \
                mock.patch.object(CLI.shutil, 'which', return_value='wt.exe'):
            CLI.open_tab(r'C:\work', 'agent#98', ['tj', 'run', '--', 'claude', 'a; b'], cleared=['GONE'],
                         forced={'FORCE_COLOR': '1'}, environ={'GONE': 'x', 'KEPT': 'y'}, run=run)
        command, kwargs = run.calls[0]
        self.assertEqual(['wt.exe', '--window', '0', 'new-tab', '--startingDirectory', r'C:\work', '--title',
                          'agent#98', '--suppressApplicationTitle', 'tj', 'run', '--', 'claude', 'a\\; b'], command)
        self.assertEqual({'KEPT': 'y', 'FORCE_COLOR': '1'}, kwargs['env'])

    def test_the_terminal_is_started_directly_and_never_through_the_hidden_launcher(self):
        for function in (CLI.open_tab, CLI.launch):
            self.assertIs(subprocess.run, inspect.signature(function).parameters['run'].default)

    def test_tmux_opens_a_named_window_that_keeps_its_name(self):
        run = fake_run([('@7', '', 0)])
        with mock.patch.object(CLI, 'IS_WINDOWS', False):
            CLI.open_tab('/work', 'agent#98', ['tj', 'run'], cleared=['NO_COLOR'], forced={'A': '1'},
                         environ={'TMUX': '/tmp/tmux'}, run=run)
        self.assertEqual(['tmux', 'new-window', '-n', 'agent#98', '-P', '-F', '#{window_id}', '--',
                          *CLI.posix_inner_command('/work', ['NO_COLOR'], {'A': '1'}, 'tj', ['run'])],
                         run.calls[0][0])
        self.assertEqual(['automatic-rename', 'allow-rename'], [call[0][5] for call in run.calls[1:]])

    def test_posix_without_tmux_is_refused(self):
        with mock.patch.object(CLI, 'IS_WINDOWS', False), self.assertRaisesRegex(CLI.LaunchError, 'tmux'):
            CLI.open_tab('/work', 'T', ['tj'], environ={}, run=fake_run())

    def test_a_failing_terminal_is_a_launch_error(self):
        with mock.patch.object(CLI, 'IS_WINDOWS', False), self.assertRaisesRegex(CLI.LaunchError, 'exit 1'):
            CLI.open_tab('/work', 'T', ['tj'], environ={'TMUX': 'x'}, run=fake_run([('', 'no server', 1)]))

    def test_the_real_terminal_is_refused_under_pytest(self):
        with mock.patch.dict(os.environ, {'PYTEST_CURRENT_TEST': 'x'}), \
                self.assertRaisesRegex(CLI.LaunchError, 'under pytest'):
            CLI.open_tab('/work', 'T', ['tj'], environ={'TMUX': 'x'})


def store(*responses):
    """An agent_store.run stand-in answering each call with the next response, raising it if an error."""
    queue = list(responses)

    def run(*arguments):
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return run


class AwaitLaunchTests(unittest.TestCase):
    def wait(self, *responses, want_session=False):
        clock = iter(range(1000))
        with mock.patch.object(CLI.agent_store, 'run', store(*responses)):
            return CLI.await_launch('L', want_session=want_session, timeout=3, session_timeout=6,
                                    clock=lambda: next(clock), wait=lambda seconds: None)

    def test_returns_once_tj_run_records_the_launch(self):
        missing = CLI.agent_store.AgentStoreError('not-found', 'no launch')
        shown = {'launch': {'state': 'running'}, 'session': None}
        self.assertIs(shown, self.wait(missing, shown))

    def test_a_launch_never_recorded_times_out(self):
        missing = CLI.agent_store.AgentStoreError('not-found', 'no launch')
        with self.assertRaises(CLI.LaunchTimeout):
            self.wait(*[missing] * 10)

    def test_waits_for_the_session_when_asked(self):
        pending = {'launch': {'state': 'running'}, 'session': None}
        bound = {'launch': {'state': 'running'}, 'session': {'native_id': 'n'}}
        self.assertIs(bound, self.wait(pending, pending, bound, want_session=True))

    def test_an_agent_that_exits_before_its_session_is_a_launch_error(self):
        ended = {'launch': {'state': 'ended'}, 'session': None}
        with self.assertRaisesRegex(CLI.LaunchError, 'exited'):
            self.wait(ended, want_session=True)

    def test_another_store_failure_is_a_launch_error(self):
        with self.assertRaisesRegex(CLI.LaunchError, 'could not read'):
            self.wait(CLI.agent_store.AgentStoreError('unexpected', 'boom'))


class LaunchTests(unittest.TestCase):
    def test_the_tab_root_is_tj_run_carrying_the_launch_identity(self):
        adapter = mock.Mock(TAB_CLEARED=(), TAB_FORCED={}, SESSION_ENVIRONMENT=(), executable=lambda: 'claude')
        with mock.patch.object(CLI, 'host_launch', return_value=adapter), \
                mock.patch.object(CLI.shutil, 'which', return_value='tj'), \
                mock.patch.object(CLI.uuid, 'uuid4', return_value=mock.Mock(hex='L1')), \
                mock.patch.object(CLI, 'open_tab') as open_tab, \
                mock.patch.object(CLI, 'await_launch', return_value={'launch': {}}) as await_launch:
            CLI.launch(CLI.Host.CLAUDE, Path('/work'), 'agent#98', ['--name', 'agent#98', 'go'],
                       item='98', parent='claude:p')
        command = open_tab.call_args.args[2]
        self.assertEqual(['tj', 'run', '--host', 'claude', '--title', 'agent#98', '--cwd', str(Path('/work')),
                          '--item', '98', '--launch-id', 'L1', '--parent', 'claude:p', '--',
                          'claude', '--name', 'agent#98', 'go'], command)
        await_launch.assert_called_once_with('L1', want_session=False)

    def test_tj_missing_is_a_launch_error(self):
        adapter = mock.Mock(TAB_CLEARED=(), TAB_FORCED={})
        with mock.patch.object(CLI, 'host_launch', return_value=adapter), \
                mock.patch.object(CLI.shutil, 'which', return_value=None), \
                self.assertRaisesRegex(CLI.LaunchError, 'tj is not on PATH'):
            CLI.launch(CLI.Host.CODEX, Path('/work'), 'T', [])


class ParentTests(unittest.TestCase):
    def test_an_explicit_parent_overrides_the_calling_session(self):
        environ = {CLI.Host.CLAUDE.session_variable: 'abc'}
        with mock.patch.dict(os.environ, environ, clear=True):
            self.assertEqual('codex:x', CLI.parent_session('codex:x'))

    def test_the_calling_session_is_the_default_parent(self):
        environ = {CLI.Host.CLAUDE.session_variable: 'abc'}
        with mock.patch.dict(os.environ, environ, clear=True):
            self.assertEqual('claude:abc', CLI.parent_session(None))

    def test_outside_any_session_there_is_no_parent(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(CLI.parent_session(None))

    def test_a_recorded_launchs_bound_session_wins_over_the_session_variable(self):
        environ = {CLI.LAUNCH_ID_ENVIRONMENT: 'L1', CLI.Host.CODEX.session_variable: 'thread-9'}
        shown = {'launch': {}, 'session': {'session': 'claude:abc'}}
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(CLI.agent_store, 'run', return_value=shown) as run:
            self.assertEqual('claude:abc', CLI.parent_session(None))
        run.assert_called_once_with('show', '--launch-id', 'L1')

    def test_an_unrecorded_launch_falls_back_to_the_session_variable(self):
        environ = {CLI.LAUNCH_ID_ENVIRONMENT: 'L1', CLI.Host.CODEX.session_variable: 'thread-9'}
        not_found = CLI.agent_store.AgentStoreError('not-found', 'no launch')
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(CLI.agent_store, 'run', side_effect=not_found):
            self.assertEqual('codex:thread-9', CLI.parent_session(None))

    def test_a_launch_not_yet_bound_to_a_session_falls_back_to_the_session_variable(self):
        environ = {CLI.LAUNCH_ID_ENVIRONMENT: 'L1', CLI.Host.CLAUDE.session_variable: 'abc'}
        shown = {'launch': {}, 'session': None}
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(CLI.agent_store, 'run', return_value=shown):
            self.assertEqual('claude:abc', CLI.parent_session(None))

    def test_an_agent_store_failure_other_than_not_found_propagates(self):
        environ = {CLI.LAUNCH_ID_ENVIRONMENT: 'L1'}
        boom = CLI.agent_store.AgentStoreError('unexpected', 'boom')
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(CLI.agent_store, 'run', side_effect=boom), \
                self.assertRaises(CLI.LaunchError):
            CLI.parent_session(None)

    def test_the_standing_rule_names_the_parent_by_title(self):
        with mock.patch.object(CLI.agent_store, 'run', return_value={'session': {'title': 'agent#98'}}):
            rule = CLI.parent_rule('claude:p')
        self.assertIn('"agent#98" (claude:p)', rule)
        self.assertIn('agent:message', rule)
        self.assertEqual('', CLI.parent_rule(None))


class ResolveItemTests(unittest.TestCase):
    def test_a_given_item_passes_through_unchanged(self):
        self.assertEqual('98', CLI.resolve_item('98', 'title', Path('/work'), 'claude:p', []))

    def test_item_fields_with_a_given_item_are_refused(self):
        with self.assertRaisesRegex(CLI.LaunchError, '--item-field applies only'):
            CLI.resolve_item('98', 'title', Path('/work'), None, [('attribution', 'Techops')])

    def test_an_absent_item_is_created_under_the_parents_item_with_its_fields(self):
        with mock.patch.object(CLI.work_item, 'parent_item_ref', return_value='42') as parent_ref, \
                mock.patch.object(CLI.work_item, 'create', return_value='99') as create:
            self.assertEqual('99', CLI.resolve_item(None, 'title', Path('/work'), 'claude:p',
                                                    [('attribution', 'Techops')]))
        parent_ref.assert_called_once_with('claude:p')
        create.assert_called_once_with(Path('/work'), 'title', '42', {'attribution': 'Techops'})

    def test_an_absent_item_with_no_parent_is_created_with_no_parent_ref(self):
        with mock.patch.object(CLI.work_item, 'parent_item_ref') as parent_ref, \
                mock.patch.object(CLI.work_item, 'create', return_value='7') as create:
            self.assertEqual('7', CLI.resolve_item(None, 'title', Path('/work'), None, []))
        parent_ref.assert_not_called()
        create.assert_called_once_with(Path('/work'), 'title', None, {})

    def test_an_item_field_needs_a_name_and_a_value(self):
        self.assertEqual(('attribution', 'User Management'), CLI.item_field('attribution=User Management'))
        for value in ('attribution', '=Techops', 'attribution='):
            with self.subTest(value=value), self.assertRaises(CLI.argparse.ArgumentTypeError):
                CLI.item_field(value)


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


if __name__ == '__main__':
    unittest.main()
