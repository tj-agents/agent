"""Synthetic process tables only; no real process is enumerated or terminated."""

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'reap_orphans', ROOT / '.agents/agent/runtime/reap_orphans.py'
)
REAPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REAPER)

HOUR = 3600
NOW = 1_800_000_000.0


def process(pid, parent_pid, name, started_at, console_host_pid=None, private_bytes=0, terminal_pid=None):
    return REAPER.Process(
        pid=pid,
        parent_pid=parent_pid,
        name=name,
        started_at=started_at,
        private_bytes=private_bytes,
        console_host_pid=console_host_pid,
        terminal_pid=terminal_pid,
    )


class ReaperTestCase(unittest.TestCase):
    """A machine where pid 500 is this session: terminal 100 -> claude 200 -> python 500."""

    def setUp(self):
        self.terminal = process(100, 1, 'WindowsTerminal.exe', NOW - 40 * HOUR)
        self.own_console = process(110, 100, 'OpenConsole.exe', NOW - 30 * HOUR)
        self.own_claude = process(200, 100, 'claude.exe', NOW - 30 * HOUR, console_host_pid=110)
        self.own_python = process(500, 200, 'python.exe', NOW - 10)
        self.base = [self.terminal, self.own_console, self.own_claude, self.own_python]

    def orphans(self, *extra, self_pid=500, names=('claude',), grace=REAPER.GRACE_SECONDS):
        table = self.base + list(extra)
        return [found.pid for found in REAPER.find_orphans(table, self_pid, NOW, names, grace)]


class DeadParentTests(ReaperTestCase):
    def test_a_process_whose_parent_is_gone_is_reaped(self):
        self.assertEqual([900], self.orphans(process(900, 8888, 'claude.exe', NOW - 30 * HOUR)))

    def test_a_process_whose_parent_is_alive_is_left_alone(self):
        shell = process(300, 100, 'powershell.exe', NOW - 31 * HOUR)
        live = process(900, 300, 'claude.exe', NOW - 30 * HOUR)
        self.assertEqual([], self.orphans(shell, live))

    def test_a_parent_pid_reused_after_the_child_started_counts_as_gone(self):
        younger = process(300, 100, 'powershell.exe', NOW - 2 * HOUR)
        orphan = process(900, 300, 'claude.exe', NOW - 30 * HOUR)
        self.assertEqual([900], self.orphans(younger, orphan))

    def test_an_unreadable_parent_start_time_keeps_the_process(self):
        opaque = process(300, 100, 'powershell.exe', None)
        child = process(900, 300, 'claude.exe', NOW - 30 * HOUR)
        self.assertEqual([], self.orphans(opaque, child))

    def test_an_unreadable_own_start_time_keeps_the_process(self):
        self.assertEqual([], self.orphans(process(900, 8888, 'claude.exe', None)))


class DeadConsoleTests(ReaperTestCase):
    def test_a_live_parent_does_not_save_a_process_whose_console_host_is_gone(self):
        """The `wt.exe` shape: WindowsTerminal outlives the tab, so only the console host dies."""
        orphan = process(900, 100, 'claude.exe', NOW - 30 * HOUR, console_host_pid=7777)
        self.assertEqual([900], self.orphans(orphan))

    def test_a_console_host_started_after_the_process_is_still_its_host(self):
        """A console host is created *for* the process, so it routinely starts a moment later."""
        host = process(7777, 100, 'conhost.exe', NOW - 30 * HOUR + 1)
        attached = process(900, 100, 'claude.exe', NOW - 30 * HOUR, console_host_pid=7777)
        self.assertEqual([], self.orphans(host, attached))

    def test_a_process_with_no_console_at_all_is_not_treated_as_orphaned(self):
        headless = process(900, 100, 'claude.exe', NOW - 30 * HOUR, console_host_pid=0)
        self.assertEqual([], self.orphans(headless))

    def test_an_unknown_console_host_is_not_treated_as_orphaned(self):
        unknown = process(900, 100, 'claude.exe', NOW - 30 * HOUR, console_host_pid=None)
        self.assertEqual([], self.orphans(unknown))


class LinuxTerminalOwnershipTests(ReaperTestCase):
    def test_reparented_agent_with_gone_session_leader_is_reaped(self):
        reparented = process(900, 100, 'claude', NOW - 30 * HOUR, terminal_pid=7777)
        self.assertEqual([900], self.orphans(reparented))

    def test_live_session_leader_keeps_reparented_agent(self):
        leader = process(7777, 1, 'bash', NOW - 31 * HOUR, terminal_pid=7777)
        reparented = process(900, 100, 'claude', NOW - 30 * HOUR, terminal_pid=7777)
        self.assertEqual([], self.orphans(leader, reparented))

    def test_missing_terminal_identity_is_not_a_kill_signal(self):
        reparented = process(900, 100, 'claude', NOW - 30 * HOUR)
        self.assertEqual([], self.orphans(reparented))

    @unittest.skipUnless(__import__('sys').platform == 'linux', '/proc is required')
    def test_linux_backend_reads_this_process_from_proc(self):
        table = REAPER.linux_process_table()
        current = [item for item in table if item.pid == __import__('os').getpid()]
        self.assertEqual(1, len(current))
        self.assertIsNotNone(current[0].started_at)
        self.assertIsInstance(current[0].terminal_pid, int)

    def test_read_table_selects_linux_backend(self):
        synthetic = [process(1, 0, 'init', NOW)]
        with mock.patch.object(REAPER.sys, 'platform', 'linux'), mock.patch.object(REAPER, 'linux_process_table', return_value=synthetic):
            self.assertEqual(synthetic, REAPER.read_process_table())

    def test_permission_denied_owner_stat_is_preserved_as_opaque(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc_root = Path(temporary)
            (proc_root / '700').mkdir()
            with mock.patch.object(Path, 'read_text', side_effect=PermissionError('denied')):
                table = REAPER.linux_process_table(proc_root, boot=NOW, ticks=100)
        self.assertEqual([700], [item.pid for item in table])
        self.assertIsNone(table[0].started_at)

    def test_malformed_owner_stat_is_preserved_as_opaque(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc_root = Path(temporary)
            stat = proc_root / '700' / 'stat'
            stat.parent.mkdir()
            stat.write_text('700 (claude) R incomplete', encoding='utf-8')
            table = REAPER.linux_process_table(proc_root, boot=NOW, ticks=100)
        self.assertEqual([700], [item.pid for item in table])
        self.assertIsNone(table[0].name)

    def test_a_truly_vanished_proc_entry_is_omitted(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc_root = Path(temporary)
            stat = proc_root / '700' / 'stat'
            stat.parent.mkdir()

            def vanish(*args, **kwargs):
                stat.parent.rmdir()
                raise FileNotFoundError()

            with mock.patch.object(Path, 'read_text', side_effect=vanish):
                table = REAPER.linux_process_table(proc_root, boot=NOW, ticks=100)
        self.assertEqual([], table)

    def test_an_unavailable_proc_snapshot_is_not_an_empty_table(self):
        proc_root = mock.Mock()
        proc_root.iterdir.side_effect = PermissionError('denied')
        self.assertIsNone(REAPER.linux_process_table(proc_root, boot=NOW, ticks=100))

    def test_an_opaque_session_leader_keeps_its_peer(self):
        leader = process(7777, None, None, None)
        reparented = process(900, 100, 'claude', NOW - 30 * HOUR, terminal_pid=7777)
        self.assertEqual([], self.orphans(leader, reparented))


class GracePeriodTests(ReaperTestCase):
    def test_a_fully_orphaned_process_inside_the_grace_period_survives(self):
        self.assertEqual([], self.orphans(process(900, 8888, 'claude.exe', NOW - 60)))

    def test_the_same_process_is_reaped_once_it_is_past_the_grace_period(self):
        starting = process(900, 8888, 'claude.exe', NOW - 60)
        self.assertEqual([900], self.orphans(starting, grace=30))

    def test_a_start_time_in_the_future_survives(self):
        skewed = process(900, 8888, 'claude.exe', NOW + 5 * HOUR)
        self.assertEqual([], self.orphans(skewed))


class OwnTreeTests(ReaperTestCase):
    def test_the_session_that_runs_the_reaper_is_never_reaped(self):
        """`own_claude` is old, its parent outlives tabs and its console host is removed here."""
        table = [self.terminal, self.own_claude, self.own_python]
        found = REAPER.find_orphans(table, 500, NOW, ('claude',), REAPER.GRACE_SECONDS)
        self.assertEqual([], [process.pid for process in found])
        self.assertIn(200, REAPER.own_tree(500, table))

    def test_an_ancestor_is_protected_even_when_fully_orphaned(self):
        table = [process(200, 8888, 'claude.exe', NOW - 30 * HOUR, console_host_pid=7777), self.own_python]
        self.assertEqual([], [found.pid for found in REAPER.find_orphans(
            table, 500, NOW, ('claude',), REAPER.GRACE_SECONDS)])

    def test_a_descendant_is_protected_even_when_fully_orphaned(self):
        child = process(600, 500, 'claude.exe', NOW - 30 * HOUR, console_host_pid=7777)
        self.assertEqual([], self.orphans(child))

    def test_a_sibling_session_outside_the_tree_is_still_judged(self):
        sibling = process(900, 8888, 'claude.exe', NOW - 30 * HOUR)
        self.assertEqual([900], self.orphans(sibling))

    def test_the_protected_set_survives_a_parent_cycle(self):
        table = [process(10, 11, 'a.exe', NOW), process(11, 10, 'b.exe', NOW)]
        self.assertEqual({10, 11}, REAPER.own_tree(10, table))

    def test_a_process_absent_from_the_table_still_protects_itself(self):
        self.assertEqual({999}, REAPER.own_tree(999, []))

    def test_an_incomplete_self_ancestry_disables_candidates(self):
        table = [
            process(500, None, None, None),
            process(900, 8888, 'claude.exe', NOW - 30 * HOUR),
        ]
        with mock.patch.object(REAPER, 'terminate') as terminate:
            code = REAPER.run_report(
                table, 500, NOW, ('claude',), REAPER.GRACE_SECONDS, True,
                stream=io.StringIO(), enrich=list,
            )
        self.assertEqual(REAPER.EXIT_OK, code)
        terminate.assert_not_called()


class ProcessSelectionTests(ReaperTestCase):
    def test_the_default_name_matches_the_windows_executable(self):
        self.assertEqual([900], self.orphans(process(900, 8888, 'claude.exe', NOW - 30 * HOUR)))

    def test_an_unnamed_process_is_never_reaped(self):
        self.assertEqual([], self.orphans(process(900, 8888, 'dotnet.exe', NOW - 30 * HOUR)))

    def test_the_process_list_is_the_extension_point(self):
        host = process(900, 8888, 'dotnet.exe', NOW - 30 * HOUR)
        self.assertEqual([900], self.orphans(host, names=('claude', 'dotnet')))

    def test_matching_ignores_case_and_extension(self):
        self.assertTrue(REAPER.name_matches('CLAUDE.EXE', {'claude'}))
        self.assertTrue(REAPER.name_matches('claude', {'claude'}))
        self.assertFalse(REAPER.name_matches('claude-helper.exe', {'claude'}))

    def test_orphans_are_reported_oldest_first(self):
        newer = process(901, 8888, 'claude.exe', NOW - 10 * HOUR)
        older = process(900, 8888, 'claude.exe', NOW - 30 * HOUR)
        self.assertEqual([900, 901], self.orphans(newer, older))


class NoticeTests(ReaperTestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic agent state ')
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.terminated = []
        original = REAPER.terminate
        REAPER.terminate = self.record_termination
        self.addCleanup(setattr, REAPER, 'terminate', original)

    def record_termination(self, process):
        self.terminated.append(process.pid)
        return None

    def notice(self, table, environ):
        stream = io.StringIO()
        code = REAPER.run_notice(
            table, 500, NOW, ('claude',), REAPER.GRACE_SECONDS,
            environ=environ, home=self.state, stream=stream, enrich=list,
        )
        return code, stream.getvalue()

    def orphaned_table(self):
        return self.base + [
            process(900, 8888, 'claude.exe', NOW - 30 * HOUR, private_bytes=500 * 1024 * 1024)
        ]

    def environ(self, **extra):
        return {REAPER.STATE_DIRECTORY_ENV: str(self.state), **extra}

    def test_the_default_notice_reports_without_terminating_anything(self):
        code, output = self.notice(self.orphaned_table(), self.environ())
        self.assertEqual(REAPER.EXIT_OK, code)
        self.assertEqual([], self.terminated)
        payload = json.loads(output)['hookSpecificOutput']
        self.assertEqual('SessionStart', payload['hookEventName'])
        self.assertIn('1 agent process whose session is gone is still resident', payload['additionalContext'])
        self.assertIn('500.00 MB', payload['additionalContext'])
        self.assertIn('--apply', payload['additionalContext'])

    def test_a_clean_machine_emits_nothing(self):
        code, output = self.notice(self.base, self.environ())
        self.assertEqual(REAPER.EXIT_OK, code)
        self.assertEqual('', output)

    def test_the_notice_is_not_repeated_within_its_interval(self):
        self.notice(self.orphaned_table(), self.environ())
        code, output = self.notice(self.orphaned_table(), self.environ())
        self.assertEqual(REAPER.EXIT_OK, code)
        self.assertEqual('', output)
        self.assertEqual([], self.terminated)

    def test_the_opt_in_variable_is_what_permits_termination(self):
        code, output = self.notice(
            self.orphaned_table(), self.environ(**{REAPER.APPLY_ENV: 'true'})
        )
        self.assertEqual(REAPER.EXIT_OK, code)
        self.assertEqual([900], self.terminated)
        self.assertIn('reaped 1 agent process', json.loads(output)['hookSpecificOutput']['additionalContext'])

    def test_an_unset_or_falsey_opt_in_never_terminates(self):
        for value in ('', '0', 'false', 'no', 'maybe'):
            with self.subTest(value=value):
                self.assertFalse(REAPER.apply_is_permitted({REAPER.APPLY_ENV: value}))
        self.assertFalse(REAPER.apply_is_permitted({}))
        self.assertTrue(REAPER.apply_is_permitted({REAPER.APPLY_ENV: ' YES '}))

    def test_a_failure_inside_the_notice_never_stops_a_session_starting(self):
        self.assertEqual(REAPER.EXIT_OK, REAPER.main(['--notice', '--process', '\0']))


class ReportTests(ReaperTestCase):
    def setUp(self):
        super().setUp()
        self.terminated = []
        original = REAPER.terminate
        REAPER.terminate = lambda process: self.terminated.append(process.pid)
        self.addCleanup(setattr, REAPER, 'terminate', original)

    def report(self, table, apply_mode):
        stream = io.StringIO()
        code = REAPER.run_report(
            table, 500, NOW, ('claude',), REAPER.GRACE_SECONDS, apply_mode,
            stream=stream, enrich=list,
        )
        return code, stream.getvalue()

    def test_the_report_names_what_it_would_reap_and_reaps_nothing(self):
        table = self.base + [process(900, 8888, 'claude.exe', NOW - 30 * HOUR)]
        code, output = self.report(table, False)
        self.assertEqual(REAPER.EXIT_OK, code)
        self.assertEqual([], self.terminated)
        self.assertIn('would reap 1 of 1 orphaned process', output)
        self.assertIn('--apply', output)

    def test_a_clean_machine_says_so(self):
        code, output = self.report(self.base, False)
        self.assertEqual(REAPER.EXIT_OK, code)
        self.assertIn('no orphaned agent processes', output)

    def test_a_refused_termination_is_reported_and_changes_the_exit_code(self):
        REAPER.terminate = lambda process: 'pid was reused'
        table = self.base + [process(900, 8888, 'claude.exe', NOW - 30 * HOUR)]
        code, output = self.report(table, True)
        self.assertEqual(REAPER.EXIT_TERMINATION, code)
        self.assertIn('FAILED pid was reused', output)


class FreshIdentityTests(unittest.TestCase):
    def test_reused_linux_identity_is_not_signalled(self):
        candidate = process(900, 8888, 'claude', NOW - 30 * HOUR)
        fresh = process(900, 8888, 'claude', NOW - 2 * HOUR)
        with mock.patch.object(REAPER.sys, 'platform', 'linux'), mock.patch.object(
            REAPER, 'linux_process_table', return_value=[fresh]
        ), mock.patch.object(REAPER.os, 'kill') as kill:
            self.assertEqual('pid was reused', REAPER.terminate(candidate))
        kill.assert_not_called()

    def test_unknown_linux_identity_is_not_signalled(self):
        candidate = process(900, 8888, 'claude', NOW - 30 * HOUR)
        fresh = process(900, None, None, None)
        with mock.patch.object(REAPER.sys, 'platform', 'linux'), mock.patch.object(
            REAPER, 'linux_process_table', return_value=[fresh]
        ), mock.patch.object(REAPER.os, 'kill') as kill:
            self.assertEqual('cannot confirm identity', REAPER.terminate(candidate))
        kill.assert_not_called()

    def test_non_finite_candidate_identity_is_not_signalled(self):
        candidate = process(900, 8888, 'claude', float('nan'))
        with mock.patch.object(REAPER.sys, 'platform', 'linux'), mock.patch.object(REAPER.os, 'kill') as kill:
            self.assertEqual('cannot confirm identity', REAPER.terminate(candidate))
        kill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
