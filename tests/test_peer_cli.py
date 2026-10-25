import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

PATH = Path(__file__).resolve().parents[1] / ".agents/agent/utility/peer/scripts/peer_cli.py"
sys.path.insert(0, str(PATH.parent))
spec = importlib.util.spec_from_file_location("peer_cli", PATH); peer_cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(peer_cli)

class PeerCliTests(unittest.TestCase):
    def test_windows_inventory_uses_runtime_ids_when_automation_ids_are_empty(self):
        inventory = json.dumps([
            {'title': 'same launch title', 'runtimeId': '42:11'},
            {'title': 'same launch title', 'runtimeId': '42:12'},
        ])
        completed = subprocess.CompletedProcess([], 0, stdout=inventory, stderr='')
        with mock.patch.object(peer_cli.close_tab, 'is_windows', return_value=True), \
                mock.patch.object(peer_cli.close_tab, 'run', return_value=completed):
            targets = peer_cli.close_tab.windows_inventory()

        self.assertEqual(['42:11', '42:12'], [target['identity']['runtime_id'] for target in targets])
        self.assertFalse(peer_cli.close_tab.same_target(targets[0]['identity'], targets[1]['identity']))
        self.assertTrue(all('automation_id' not in target['identity'] for target in targets))
        adapter = (PATH.parent / 'windows_terminal_uia.ps1').read_text(encoding='utf-8')
        self.assertIn('$tab.GetRuntimeId()', adapter)
        self.assertNotIn('$tab.Current.AutomationId', adapter)

    def test_windows_close_selects_the_exact_runtime_id(self):
        completed = subprocess.CompletedProcess([], 0, stdout='', stderr='')
        calls = []

        def run(arguments):
            calls.append(arguments)
            return completed

        target = {'identity': {'kind': 'windows-terminal', 'runtime_id': '42:11'}}
        with mock.patch.object(peer_cli.close_tab, 'is_windows', return_value=True), \
                mock.patch.object(peer_cli.close_tab, 'target_liveness', return_value=False), \
                mock.patch.object(peer_cli.close_tab, 'require_closed'), \
                mock.patch.object(peer_cli.close_tab, 'run', side_effect=run):
            peer_cli.close_tab.close_actual(target, force=True)

        self.assertIn('-TabId', calls[0])
        self.assertEqual('42:11', calls[0][calls[0].index('-TabId') + 1])

    def test_codex_native_session_id_prevents_peer_attachment_selection(self):
        host = peer_cli.register_session.ProcessInfo(10, 1, 'codex.exe', 20.0)
        own = {'host': 'codex', 'session_id': 'own', 'cwd': str(PATH.parents[4]),
               'pid': 10, 'pid_started_at': 20.0, 'started_at': 100.0}
        peer = {**own, 'session_id': 'peer', 'started_at': 300.0}
        with mock.patch.object(peer_cli.session_close.os, 'getppid', return_value=10), \
                mock.patch.object(peer_cli.session_close, 'process_info', return_value=host), \
                mock.patch.object(peer_cli.session_close.register_session, 'read_host_names', return_value={'codex'}), \
                mock.patch.object(peer_cli.session_close, 'entries', side_effect=lambda: iter([('own.json', own), ('peer.json', peer)])), \
                mock.patch.object(peer_cli.session_close, 'verified_live', return_value=True), \
                mock.patch.dict(peer_cli.session_close.os.environ, {'CODEX_THREAD_ID': 'own'}, clear=False):
            self.assertEqual('own', peer_cli.session_close.own_host_and_entry(PATH.parents[4])['session_id'])
            self.assertEqual('peer', peer_cli.session_close.own_host_and_entry(PATH.parents[4], session_id='peer')['session_id'])

        with mock.patch.object(peer_cli.session_close.os, 'getppid', return_value=10), \
                mock.patch.object(peer_cli.session_close, 'process_info', return_value=host), \
                mock.patch.object(peer_cli.session_close.register_session, 'read_host_names', return_value={'codex'}), \
                mock.patch.object(peer_cli.session_close, 'entries', side_effect=lambda: iter([('own.json', own), ('peer.json', peer)])), \
                mock.patch.object(peer_cli.session_close, 'verified_live', return_value=True), \
                mock.patch.dict(peer_cli.session_close.os.environ, {'CODEX_THREAD_ID': 'missing'}, clear=False):
            with self.assertRaisesRegex(peer_cli.session_close.Refusal, 'cannot resolve'):
                peer_cli.session_close.own_host_and_entry(PATH.parents[4])

        with mock.patch.object(peer_cli.session_close.os, 'getppid', return_value=10), \
                mock.patch.object(peer_cli.session_close, 'process_info', return_value=host), \
                mock.patch.object(peer_cli.session_close.register_session, 'read_host_names', return_value={'codex'}), \
                mock.patch.object(peer_cli.session_close, 'entries', side_effect=lambda: iter([('own.json', own), ('peer.json', peer)])), \
                mock.patch.object(peer_cli.session_close, 'verified_live', return_value=True), \
                mock.patch.dict(peer_cli.session_close.os.environ, {'CODEX_THREAD_ID': ''}, clear=False):
            with self.assertRaisesRegex(peer_cli.session_close.Refusal, 'multiple verified'):
                peer_cli.session_close.own_host_and_entry(PATH.parents[4])

    def test_ambiguous_prefix_refuses(self):
        entries = [
            {"title": "one", "session_id": "a"},
            {"title": "once", "session_id": "b"},
        ]
        with self.assertRaises(peer_cli.session_close.Refusal):
            peer_cli.select(entries, "on")

    def test_exact_session_id_wins(self):
        entries = [
            {"title": "one", "session_id": "a"},
            {"title": "once", "session_id": "b"},
        ]
        self.assertEqual("b", peer_cli.select(entries, "b")["session_id"])

    def test_unrecorded_uses_cross_platform_process_table(self):
        process = peer_cli.register_session.ProcessInfo(88, 1, "codex.exe", 2.0)
        with mock.patch.object(peer_cli.register_session, "process_table", return_value={88: process}):
            found = peer_cli.unrecorded([])
        self.assertEqual(found[0]["pid"], 88)
        self.assertFalse(found[0]["recorded"])

    def test_force_closes_a_recorded_stale_terminal_without_killing_a_pid(self):
        stale = {
            "title": "old tab",
            "session_id": "stale",
            "alive": False,
            "terminal": {"kind": "windows-terminal"},
        }
        with mock.patch.object(peer_cli, "records", return_value=[stale]):
            with mock.patch.object(peer_cli.close_tab, "close_stale_entry") as close_terminal, \
                    mock.patch.object(peer_cli.os, "kill") as killer:
                self.assertEqual(peer_cli.main(["close", "stale", "--force"]), 0)
        close_terminal.assert_called_once_with(stale)
        killer.assert_not_called()

    def test_windows_uia_unavailable_refuses_an_unverified_host_only_close(self):
        target = {
            "title": "peer", "session_id": "peer", "pid": 31, "pid_started_at": 4.0,
            "terminal": {"kind": "windows-terminal"},
        }
        with mock.patch.object(peer_cli.close_tab, "close_entry",
                               side_effect=peer_cli.close_tab.TerminalUnavailable("no UIA")), \
                mock.patch.object(peer_cli.session_close, "verified_live", return_value=True), \
                mock.patch.object(peer_cli.os, "kill") as killer:
            with self.assertRaisesRegex(peer_cli.session_close.Refusal, "actual-tab closure"):
                peer_cli.close_terminal_or_host(target, force=False)
        killer.assert_not_called()

    def test_windows_ambiguous_tab_never_uses_host_only_fallback(self):
        target = {
            "title": "peer", "session_id": "peer", "pid": 31, "pid_started_at": 4.0,
            "terminal": {"kind": "windows-terminal"},
        }
        with mock.patch.object(peer_cli.close_tab, "close_entry",
                               side_effect=peer_cli.close_tab.TerminalRefusal("ambiguous")), \
                mock.patch.object(peer_cli.os, "kill") as killer:
            with self.assertRaisesRegex(peer_cli.close_tab.TerminalRefusal, "ambiguous"):
                peer_cli.close_terminal_or_host(target, force=False)
        killer.assert_not_called()

    def test_unknown_identity_never_signals_a_process_even_with_force(self):
        target = {
            "title": "unknown", "session_id": "unknown", "pid": 31,
            "pid_started_at": 4.0, "alive": None,
            "terminal": {"kind": "windows-terminal"},
        }
        with mock.patch.object(peer_cli, "records", return_value=[target]), \
                mock.patch.object(peer_cli.os, "kill") as killer:
            with self.assertRaisesRegex(peer_cli.session_close.Refusal, "unknown"):
                peer_cli.main(["close", "unknown", "--force"])
        killer.assert_not_called()

    def test_recorded_host_without_terminal_identity_cannot_be_reported_closed(self):
        target = {
            "title": "missing tab", "session_id": "missing-tab", "pid": 31,
            "pid_started_at": 4.0, "alive": True,
        }
        with mock.patch.object(peer_cli, "records", return_value=[target]), \
                mock.patch.object(peer_cli.session_close, "verified_live", return_value=True), \
                mock.patch.object(peer_cli.os, "kill") as killer:
            with self.assertRaisesRegex(peer_cli.session_close.Refusal, "terminal identity"):
                peer_cli.main(["close", "missing-tab", "--force"])
        killer.assert_not_called()

    def test_list_is_json_and_inspection_leaves_activity_unobserved(self):
        target = {"host": "codex", "session_id": "one", "title": "one", "cwd": "/repo",
                  "pid": 1, "alive": True, "recorded": True}
        other = {"host": "claude", "session_id": "two", "title": "two", "cwd": "/repo",
                 "pid": 2, "alive": True, "recorded": True}
        with mock.patch.object(peer_cli, "records", return_value=[target, other]), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(0, peer_cli.main(["list", "--all", "--host", "codex"]))
            self.assertEqual([target], json.loads(stdout.getvalue()))
        with mock.patch.object(peer_cli, "records", return_value=[target]), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(0, peer_cli.main(["inspect", "one"]))
            value = json.loads(stdout.getvalue())
        self.assertEqual("unobserved", value["activity"])
        self.assertIn("metadata", value["provenance"])
