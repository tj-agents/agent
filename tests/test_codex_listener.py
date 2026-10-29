import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/agent/.agents/agent/runtime'))
from codex import listener


THREAD = '00000000-0000-4000-8000-000000000041'
BACKEND = {'pid': 123, 'process_start_time': 'pinned-start'}


class NativeServer:
    def __init__(self, cwd):
        self.thread = {'id': THREAD, 'cwd': str(cwd), 'source': 'cli',
                       'status': {'type': 'active'}, 'canAcceptDirectInput': True}
        self.pages = {None: {'data': [THREAD]}}
        self.calls = []
        self.submission_error = None
        self.submission_check = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def request(self, method, params):
        self.calls.append((method, params))
        if method == 'thread/read':
            return {'thread': self.thread}
        if method == 'thread/loaded/list':
            return self.pages[params.get('cursor')]
        if method == 'turn/start':
            if self.submission_check:
                self.submission_check()
            if self.submission_error:
                raise self.submission_error
            return {'turn': {'id': 'native-accepted-turn'}}
        raise AssertionError(method)

    def submissions(self):
        return [params for method, params in self.calls if method == 'turn/start']


class CodexListenerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='listener café spaces ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / 'state'
        self.profile = self.root / 'profile'
        self.command_cwd = self.root / 'command café'
        self.thread_cwd = self.root / 'thread directory'
        for path in (self.command_cwd, self.thread_cwd, self.profile, self.state / 'cli-sessions'):
            path.mkdir(parents=True)
        self.record = {'host': 'codex', 'session_id': THREAD, 'cwd': str(self.thread_cwd),
                       'profile': str(self.profile), 'backend': dict(BACKEND),
                       'client_message_id': 'original-user-input'}
        self.write_registration()
        self.patch(mock.patch.dict(os.environ, {'CODEX_THREAD_ID': THREAD,
                                               'CODEX_HOME': str(self.profile),
                                               'AGENT_STATE_DIRECTORY': str(self.state)}))
        self.daemon = self.patch(mock.patch.object(listener.client, 'daemon_identity',
                                                 return_value=dict(BACKEND)))
        self.arguments = self.patch(mock.patch.object(listener.client, 'process_arguments',
                                                    return_value=[sys.executable, 'app-server']))
        self.verify_capability = listener.verify_capability
        self.capability = self.patch(mock.patch.object(listener, 'verify_capability'))
        self.server = NativeServer(self.thread_cwd)
        self.connection = self.patch(mock.patch.object(listener, 'connection', return_value=self.server))
        self.launch = self.patch(mock.patch.object(listener, 'detach', side_effect=listener.worker))
        self.patch(mock.patch.object(listener.client, 'current_input',
                                     side_effect=AssertionError('A listener must survive new user input')))
        self.script = self.command_cwd / 'command.py'

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def write_registration(self):
        (self.state / 'cli-sessions' / (THREAD + '.json')).write_text(
            json.dumps(self.record), encoding='utf-8')

    def command(self, source, *arguments):
        self.script.write_text(source, encoding='utf-8')
        return (sys.executable, '-B', str(self.script), *arguments)

    def start(self, command, identity='exact-job', timeout=5):
        return listener.start(identity, 'Wait for exact deterministic operation',
                              self.command_cwd, timeout, command)

    def job(self, identity='exact-job'):
        return self.state / 'listeners' / listener.job_key(THREAD, identity)

    def test_duplicate_identity_and_duplicate_worker_execute_command_once(self):
        command = self.command(
            "from pathlib import Path\np = Path('count')\n"
            "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n")
        first = self.start(command)
        second = self.start(command)
        third = listener.worker(self.job())
        self.assertEqual(['accepted'] * 3, [row['status'] for row in (first, second, third)])
        self.assertEqual('1', (self.command_cwd / 'count').read_text())
        self.assertEqual(1, self.launch.call_count)
        self.assertEqual(1, len(self.server.submissions()))
        self.assertFalse(first['acknowledged'])

    def test_reused_identity_with_different_request_is_refused(self):
        command = self.command('print("one")\n')
        self.start(command)
        with self.assertRaisesRegex(RuntimeError, 'different request'):
            self.start(command + ('different argument',))
        self.assertEqual(1, self.launch.call_count)

    def test_success_preserves_unicode_argv_and_command_directory(self):
        arguments = ('two words', 'café 雪', 'quotes " and spaces', '')
        command = self.command(
            'import json, os, sys\n'
            "sys.stdout.buffer.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}, "
            "ensure_ascii=False).encode('utf-8'))\n", *arguments)
        result = self.start(command)
        self.assertEqual('success', result['outcome'])
        self.assertEqual(0, result['returncode'])
        output = json.loads(Path(result['output']).read_text(encoding='utf-8'))
        self.assertEqual(list(arguments), output['argv'])
        self.assertEqual(str(self.command_cwd), output['cwd'])
        self.assertEqual(str(self.thread_cwd), self.connection.call_args.args[0].cwd)

    def test_nonzero_spawn_failure_and_timeout_all_submit_a_result(self):
        cases = [('nonzero', 'import sys\nsys.exit(7)\n', 5, 'failed', 7),
                 ('timeout', 'import time\ntime.sleep(10)\n', .1, 'timeout', None)]
        for identity, source, timeout, outcome, returncode in cases:
            with self.subTest(identity=identity):
                started = time.monotonic()
                result = self.start(self.command(source), identity, timeout)
                self.assertEqual('accepted', result['status'])
                self.assertEqual(outcome, result['outcome'])
                if returncode is not None:
                    self.assertEqual(returncode, result['returncode'])
                self.assertLess(time.monotonic() - started, 5)
        result = self.start((str(self.root / 'missing-executable'),), 'missing')
        self.assertEqual(('accepted', 'failed', None),
                         (result['status'], result['outcome'], result['returncode']))
        self.assertEqual(3, len(self.server.submissions()))

    def test_timeout_kills_a_command_that_ignores_termination(self):
        target = listener.Target(THREAD, str(self.profile), str(self.thread_cwd),
                                 dict(BACKEND), sys.executable)
        request = listener.Request(target, 'stubborn-command', 'Bound a stuck command',
                                   str(self.command_cwd), .1, ('stubborn-command',))
        process = mock.Mock(returncode=-9)
        process.wait.side_effect = [subprocess.TimeoutExpired(request.command, .1),
                                    subprocess.TimeoutExpired(request.command, 2), -9]
        with mock.patch.object(listener.subprocess, 'Popen', return_value=process):
            result = listener.run_command(request, self.root / 'output.log')
        self.assertEqual({'outcome': 'timeout', 'returncode': -9}, result)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual([mock.call(timeout=.1), mock.call(timeout=2), mock.call(timeout=5)],
                         process.wait.call_args_list)

    def test_active_and_idle_use_only_tool_output_and_survive_new_user_input(self):
        command = self.command('print("complete")\n')
        for state in ('active', 'idle'):
            with self.subTest(state=state):
                self.server.thread['status'] = {'type': state}
                original = listener.run_command

                def run_and_update(request, path):
                    result = original(request, path)
                    self.record['client_message_id'] = 'new-user-input'
                    self.record['native_turn_id'] = 'new-active-turn'
                    self.write_registration()
                    return result

                with mock.patch.object(listener, 'run_command', side_effect=run_and_update):
                    result = self.start(command, state)
                self.assertEqual('accepted', result['status'])
                params = self.server.submissions()[-1]
                self.assertEqual({'threadId', 'input', 'toolOutput'}, set(params))
                self.assertEqual(THREAD, params['threadId'])
                self.assertEqual([], params['input'])
                self.assertEqual({'name', 'namespace', 'output'}, set(params['toolOutput']))
                self.assertEqual('agent_listener', params['toolOutput']['name'])
                self.assertIsNone(params['toolOutput']['namespace'])
                payload = json.loads(params['toolOutput']['output'])
                self.assertEqual(state, payload['identity'])
                self.assertEqual('success', payload['outcome'])
                self.assertEqual('complete', payload['tail'].strip())
        self.assertFalse(any(method in ('turn/steer', 'thread/start', 'thread/resume')
                             for method, _ in self.server.calls))

    def test_delivering_is_durable_before_submission_and_uncertainty_never_replays(self):
        def check_state():
            self.assertEqual('delivering', listener.status(self.job())['status'])

        self.server.submission_check = check_state
        self.server.submission_error = RuntimeError('Connection lost after submission')
        command = self.command('print("finished")\n')
        result = self.start(command)
        self.assertEqual('delivery-unknown', result['status'])
        self.assertEqual('delivery-unknown', self.start(command)['status'])
        self.assertEqual('delivery-unknown', listener.worker(self.job())['status'])
        self.assertEqual(1, len(self.server.submissions()))
        self.assertEqual(1, self.launch.call_count)

    def test_mismatched_target_and_unloaded_or_subagent_threads_fail_before_command(self):
        command = self.command("from pathlib import Path\nPath('executed').touch()\n")
        original = dict(self.server.thread)
        cases = [('id', 'foreign'), ('cwd', str(self.command_cwd)),
                 ('canAcceptDirectInput', False), ('canAcceptDirectInput', None),
                 ('status', {'type': 'notLoaded'}), ('source', {'subAgent': {}}),
                 ('threadSource', 'subagent')]
        for key, value in cases:
            with self.subTest(field=key, value=value):
                self.server.thread = {**original, key: value}
                with self.assertRaisesRegex(RuntimeError, 'exact loaded CLI thread'):
                    self.start(command)
        self.server.thread = original
        self.server.pages = {None: {'data': []}}
        with self.assertRaisesRegex(RuntimeError, 'no longer loaded'):
            self.start(command)
        self.assertFalse((self.command_cwd / 'executed').exists())
        self.launch.assert_not_called()

    def test_wrong_profile_host_session_and_backend_registration_are_refused(self):
        command = self.command('print("never")\n')
        for key, value in [('profile', str(self.root / 'other-profile')), ('host', 'claude'),
                           ('session_id', 'foreign'), ('backend', {'pid': 999})]:
            with self.subTest(field=key):
                saved = self.record[key]
                self.record[key] = value
                self.write_registration()
                with self.assertRaises(RuntimeError):
                    self.start(command)
                self.record[key] = saved
        self.launch.assert_not_called()

    def test_missing_thread_id_is_refused_without_discovery_or_command(self):
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': ''}):
            with self.assertRaisesRegex(RuntimeError, 'CODEX_THREAD_ID'):
                self.start(self.command('print("never")\n'))
        self.daemon.assert_not_called()
        self.launch.assert_not_called()

    def test_backend_change_between_start_and_worker_prevents_command(self):
        def detach_with_changed_backend(job):
            self.daemon.return_value = {'pid': 999, 'process_start_time': 'replaced'}
            return listener.worker(job)

        self.launch.side_effect = detach_with_changed_backend
        result = self.start(self.command("from pathlib import Path\nPath('executed').touch()\n"))
        self.assertEqual('unavailable', result['status'])
        self.assertIn('daemon or executable changed', result['error'])
        self.assertFalse((self.command_cwd / 'executed').exists())
        self.assertEqual([], self.server.submissions())

    def test_backend_executable_change_before_callback_prevents_submission(self):
        original = listener.run_command

        def run_and_replace(request, output):
            result = original(request, output)
            self.arguments.return_value = [str(self.root / 'replacement-codex'), 'app-server']
            return result

        with mock.patch.object(listener, 'run_command', side_effect=run_and_replace):
            result = self.start(self.command('print("done")\n'))
        self.assertEqual(('unavailable', 'success'), (result['status'], result['outcome']))
        self.assertEqual([], self.server.submissions())

    def test_target_change_before_callback_prevents_submission(self):
        original = listener.run_command

        def run_and_replace(request, output):
            result = original(request, output)
            self.server.thread['cwd'] = str(self.command_cwd)
            return result

        with mock.patch.object(listener, 'run_command', side_effect=run_and_replace):
            result = self.start(self.command('print("done")\n'))
        self.assertEqual('unavailable', result['status'])
        self.assertEqual([], self.server.submissions())

    def test_loaded_thread_pagination_is_supported_and_bounded(self):
        self.server.pages = {None: {'data': ['other'], 'nextCursor': 'page-two'},
                             'page-two': {'data': [THREAD]}}
        result = self.start(self.command('print("done")\n'))
        self.assertEqual('accepted', result['status'])
        self.server.pages['page-two'] = {'data': [], 'nextCursor': 'page-two'}
        with self.assertRaisesRegex(RuntimeError, 'did not advance'):
            self.start(self.command('print("never")\n'), 'repeated-cursor')

    def test_unsupported_native_tool_output_capability_prevents_command(self):
        self.capability.side_effect = RuntimeError('Native daemon lacks toolOutput')
        with self.assertRaisesRegex(RuntimeError, 'lacks toolOutput'):
            self.start(self.command('print("never")\n'))
        self.launch.assert_not_called()

    def test_schema_probe_uses_verified_daemon_executable_and_exact_profile(self):
        target = listener.Target(THREAD, str(self.profile), str(self.thread_cwd),
                                 dict(BACKEND), str(Path(sys.executable).resolve()))
        supported = True

        def generate(arguments, **kwargs):
            self.assertEqual(target.executable, arguments[0])
            self.assertEqual(target.profile, kwargs['env']['CODEX_HOME'])
            path = Path(arguments[arguments.index('--out') + 1]) / 'v2'
            path.mkdir()
            (path / 'TurnStartParams.json').write_text(
                json.dumps({'properties': {'toolOutput': {}} if supported else {}}), encoding='utf-8')

        with mock.patch.object(listener.subprocess, 'run', side_effect=generate):
            self.verify_capability(target)
            supported = False
            with self.assertRaisesRegex(RuntimeError, 'does not support toolOutput'):
                self.verify_capability(target)


if __name__ == '__main__':
    unittest.main()
