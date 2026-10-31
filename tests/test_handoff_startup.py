import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import launcher_test_support


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HandoffStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='handoff startup é ')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.prompt = self.directory / 'prompt.txt'
        self.prompt.write_text('Continue the authorized source task.', encoding='utf-8')

    def run_launch(self, host, status, terminal_timeout=False):
        launcher = load(f'handoff_startup_{host}', ROOT / f'plugins/agent/.agents/agent/resources/handoff/launch_{host}.py')
        cli = load(f'handoff_cli_{host}', ROOT / 'plugins/agent/.agents/agent/runtime/agent_cli.py')
        cli.resolve_codex_executable = mock.Mock(return_value=launcher_test_support.CODEX_EXECUTABLE)
        cli.resolve_claude_executable = mock.Mock(return_value='/bin/claude')
        cli.launch_tab = mock.Mock()
        real_wait = cli.await_handoff_startup
        cli.await_handoff_startup = mock.Mock()
        receipt = self.directory / f'{host}.json'

        def terminal(*args, **kwargs):
            arguments = kwargs['arguments']
            self.assertEqual(str(receipt), arguments[arguments.index('--status-file') + 1])
            self.assertIn('--require-session-start', arguments)
            if status is not None:
                receipt.write_text(json.dumps(status), encoding='utf-8')
            if terminal_timeout:
                raise cli.LaunchTimeout('terminal submission timed out')

        cli.launch_tab.side_effect = terminal
        cli.handoff_status_path = lambda: receipt
        cli.await_handoff_startup.side_effect = lambda path: real_wait(path, timeout=0, wrapper_timeout=0)
        output, error = io.StringIO(), io.StringIO()
        with mock.patch.object(launcher, '_load_agent_cli', return_value=cli), contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            result = launcher.main(['--working-directory', str(self.directory), '--prompt-path', str(self.prompt), '--title', 'Test handoff', '--model', 'explicit-model'])
        cli.launch_tab.assert_called_once()
        return result, output.getvalue(), error.getvalue(), receipt

    def test_failed_refresh_never_emits_success_for_either_host(self):
        for host in ('codex', 'claude'):
            with self.subTest(host=host):
                result, output, error, receipt = self.run_launch(host, {'state': 'failed', 'error': 'marketplace unavailable'})
                self.assertEqual(1, result)
                self.assertIn('marketplace unavailable', error)
                self.assertIn(str(receipt), error)
                self.assertNotIn('Launched', output)
                self.assertNotIn('"startup": "verified"', output)

    def test_dispatcher_requires_host_and_forwards_only_the_host_arguments(self):
        dispatcher = load('handoff_dispatcher', ROOT / 'plugins/agent/.agents/agent/utility/handoff/scripts/handoff.py')
        handler = mock.Mock()
        handler.main.return_value = 7
        with mock.patch.object(dispatcher, '_load_handler', return_value=handler) as loader:
            self.assertEqual(7, dispatcher.main(['--host', 'claude', '--', '--lane', 'L4', '--title', 'exact']))
        self.assertEqual(['claude'], [str(call.args[0]) for call in loader.call_args_list])
        handler.main.assert_called_once_with(['--lane', 'L4', '--title', 'exact'])

    def test_dispatcher_refuses_unseparated_or_unsupported_host_before_loading(self):
        dispatcher = load('handoff_dispatcher_refusal', ROOT / 'plugins/agent/.agents/agent/utility/handoff/scripts/handoff.py')
        with mock.patch.object(dispatcher, '_load_handler') as loader:
            with self.assertRaises(SystemExit):
                dispatcher.main(['--host', 'claude', '--lane', 'L4'])
            with self.assertRaises(SystemExit):
                dispatcher.main(['--host', 'unknown', '--', '--lane', 'L4'])
        loader.assert_not_called()

    def test_authored_and_packaged_dispatchers_find_only_packaged_private_handlers(self):
        copies = [ROOT / 'plugins/agent/.agents/agent/utility/handoff/scripts/handoff.py',
                  ROOT / 'plugins/agent/skills/handoff/scripts/handoff.py']
        for copy in copies:
            with self.subTest(copy=str(copy.relative_to(ROOT))):
                dispatcher = load(f'handoff_dispatcher_{abs(hash(copy))}', copy)
                for host in ('claude', 'codex'):
                    handler = dispatcher._handler_path(host)
                    self.assertEqual(f'launch_{host}.py', handler.name)
                    self.assertIn('.agents', handler.parts)
                    self.assertTrue(handler.is_file())

    def test_nested_plugin_missing_a_handler_refuses_without_loading_an_enclosing_checkout(self):
        outer = self.directory / 'author-checkout'
        (outer / '.git').mkdir(parents=True)
        outer_handler = outer / '.agents/agent/resources/handoff/launch_claude.py'
        outer_handler.parent.mkdir(parents=True)
        outer_handler.write_text('def main(argv):\n    return 99\n', encoding='utf-8')
        nested = outer / 'plugins/agent'
        marker = nested / '.codex-plugin/plugin.json'
        marker.parent.mkdir(parents=True)
        marker.write_text('{}', encoding='utf-8')
        dispatcher_path = nested / 'skills/handoff/scripts/handoff.py'
        dispatcher_path.parent.mkdir(parents=True)
        dispatcher_path.write_text(
            (ROOT / 'plugins/agent/.agents/agent/utility/handoff/scripts/handoff.py').read_text(encoding='utf-8'),
            encoding='utf-8',
        )
        dispatcher = load('nested_missing_handoff_dispatcher', dispatcher_path)
        with self.assertRaisesRegex(SystemExit, 'was not found under'):
            dispatcher.main(['--host', 'claude', '--'])

    def test_private_handlers_refuse_missing_nearest_runtime_without_loading_enclosing_checkout(self):
        outer = self.directory / 'author-checkout'
        (outer / '.git').mkdir(parents=True)
        outer_runtime = outer / '.agents/agent/runtime/agent_cli.py'
        outer_runtime.parent.mkdir(parents=True)
        outer_runtime.write_text("raise AssertionError('outer runtime loaded')\n", encoding='utf-8')
        nested = outer / 'plugins/agent'
        marker = nested / '.claude-plugin/plugin.json'
        marker.parent.mkdir(parents=True)
        marker.write_text('{}', encoding='utf-8')
        (nested / '.agents/agent/runtime').mkdir(parents=True)
        for host in ('claude', 'codex'):
            with self.subTest(host=host):
                handler_path = nested / f'.agents/agent/resources/handoff/launch_{host}.py'
                handler_path.parent.mkdir(parents=True, exist_ok=True)
                handler_path.write_text(
                    (ROOT / f'plugins/agent/.agents/agent/resources/handoff/launch_{host}.py').read_text(encoding='utf-8'),
                    encoding='utf-8',
                )
                handler = load(f'nested_missing_runtime_{host}', handler_path)
                with self.assertRaisesRegex(SystemExit, 'was not found under'):
                    handler._load_agent_cli()

    def test_native_exit_before_confirmation_is_failure_even_with_zero_exit(self):
        for host in ('codex', 'claude'):
            for returncode in (0, 2):
                with self.subTest(host=host, returncode=returncode):
                    result, output, error, _ = self.run_launch(host, {'state': 'exited', 'returncode': returncode})
                    self.assertEqual(1, result)
                    self.assertIn('before handoff confirmation', error)
                    self.assertNotIn('"startup": "verified"', output)

    def test_uncertain_preparation_retains_receipt_and_never_retries(self):
        for host in ('codex', 'claude'):
            with self.subTest(host=host):
                result, output, error, receipt = self.run_launch(host, {'state': 'preparing'})
                self.assertEqual(3, result)
                self.assertEqual({'state': 'preparing'}, json.loads(receipt.read_text(encoding='utf-8')))
                self.assertIn(str(receipt), error)
                self.assertNotIn('"startup": "verified"', output)

    def test_live_session_without_acknowledgement_reports_uncertainty_without_retry(self):
        for host in ('codex', 'claude'):
            with self.subTest(host=host):
                status = {'state': 'unconfirmed', 'child_pid': 4242}
                result, output, error, receipt = self.run_launch(host, status)
                self.assertEqual(3, result)
                self.assertEqual(status, json.loads(receipt.read_text(encoding='utf-8')))
                self.assertIn('still running', error)
                self.assertIn('do not relaunch', error)
                self.assertIn(str(receipt), error)
                self.assertNotIn('"startup": "verified"', output)

    def test_missing_wrapper_is_abandoned_atomically_for_either_host(self):
        for host in ('codex', 'claude'):
            with self.subTest(host=host):
                result, output, error, receipt = self.run_launch(host, None)
                self.assertEqual(1, result)
                self.assertEqual('abandoned', json.loads(receipt.read_text(encoding='utf-8'))['state'])
                self.assertIn('late launch is blocked', error)
                self.assertNotIn('"startup": "verified"', output)

    def test_verified_native_startup_is_bound_to_the_exact_attempt(self):
        for host in ('codex', 'claude'):
            with self.subTest(host=host):
                result, output, error, receipt = self.run_launch(host, {'state': 'running'})
                self.assertEqual((0, ''), (result, error))
                event = json.loads(output.splitlines()[-1])
                self.assertEqual('verified', event['startup'])
                self.assertEqual(str(receipt), event['status_path'])
                self.assertEqual(str(self.prompt.resolve()), event['prompt_path'])

    def test_wrapper_claim_wins_abandonment_race_without_late_success(self):
        cli = load('handoff_race_cli', ROOT / 'plugins/agent/.agents/agent/runtime/agent_cli.py')
        receipt = self.directory / 'race.json'
        class Status:
            @staticmethod
            def read(path):
                return None
            @staticmethod
            def create(path, value):
                return False
        with mock.patch.object(cli.importlib.util, 'module_from_spec', return_value=Status), mock.patch.object(cli.importlib.util, 'spec_from_file_location') as spec:
            spec.return_value.loader.exec_module = lambda module: None
            with self.assertRaises(cli.LaunchTimeout):
                cli.await_handoff_startup(receipt, timeout=0, wrapper_timeout=0)

    def test_clean_exit_retains_verified_session_start_without_polling_race(self):
        for host in ('codex', 'claude'):
            with self.subTest(host=host):
                result, output, error, _ = self.run_launch(host, {'state': 'exited', 'returncode': 0, 'running_at': 100})
                self.assertEqual((0, ''), (result, error))
                self.assertIn('"startup": "verified"', output)

    def test_terminal_submission_timeout_keeps_exact_attempt_receipt(self):
        for host in ('codex', 'claude'):
            with self.subTest(host=host):
                result, output, error, receipt = self.run_launch(host, {'state': 'preparing'}, terminal_timeout=True)
                self.assertEqual(3, result)
                self.assertIn(str(receipt), error)
                self.assertTrue(receipt.is_file())
                self.assertNotIn('"startup": "verified"', output)
