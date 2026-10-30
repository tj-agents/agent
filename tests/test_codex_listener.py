from dataclasses import replace
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/agent/.agents/agent/runtime'))
from codex import listener
if os.name == 'nt':
    from codex import listener_windows


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
        if os.name == 'nt':
            self.windows_daemon_executable = listener_windows.daemon_executable

            def executable(_profile, backend):
                if backend.value() != self.daemon.return_value:
                    raise RuntimeError('The pinned native Codex daemon or executable changed')
                return self.arguments.return_value[0]

            self.patch(mock.patch.object(listener_windows, 'daemon_executable', side_effect=executable))
        self.verify_capability = listener.verify_capability
        self.capability = self.patch(mock.patch.object(listener, 'verify_capability'))
        self.verify_running_version = listener.verify_running_version
        self.version = self.patch(mock.patch.object(listener, 'verify_running_version'))
        self.verify_mapped_image = listener.verify_mapped_image
        self.patch(mock.patch.object(listener, 'verify_mapped_image'))
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

    def test_invalid_requests_are_refused_at_construction_and_after_persistence(self):
        target = listener.capture_target(self.state)
        request = listener.Request(target, 'exact-job', 'Wait for command', str(self.command_cwd),
                                   5, self.command('print("never")\n'))
        self.job().mkdir(parents=True)
        for invalid in ({'identity': ' '}, {'purpose': None}, {'cwd': 'relative'},
                        {'timeout': True}, {'timeout': float('inf')}, {'timeout': 0},
                        {'command': ()}, {'command': ('',)}, {'command': ('bad\0argument',)}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(RuntimeError):
                    replace(request, **invalid)
                value = {**request.value(), **invalid}
                (self.job() / 'request.json').write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(RuntimeError):
                    listener.load_request(self.job())
        self.launch.assert_not_called()

    def test_invalid_target_identity_is_refused_at_construction_and_after_persistence(self):
        target = listener.capture_target(self.state)
        request = listener.Request(target, 'exact-job', 'Wait for command', str(self.command_cwd),
                                   5, (sys.executable, '--version'))
        self.job().mkdir(parents=True)
        for invalid in ({'session_id': 'foreign'}, {'profile': 'relative'}, {'cwd': ''},
                        {'executable': 'relative'}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(RuntimeError):
                    replace(target, **invalid)
                value = request.value()
                value['target'].update(invalid)
                (self.job() / 'request.json').write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(RuntimeError):
                    listener.load_request(self.job())
        for invalid in ({'pid': True}, {'pid': 0}, {'process_start_time': ''}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(RuntimeError):
                    replace(target.backend, **invalid)
                value = request.value()
                value['target']['backend'].update(invalid)
                (self.job() / 'request.json').write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(RuntimeError):
                    listener.load_request(self.job())
        self.launch.assert_not_called()

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

    def test_timeout_terminates_a_real_descendant_before_it_can_write_later(self):
        child = self.command_cwd / 'descendant.py'
        child.write_text(
            "import os, signal, time\nfrom pathlib import Path\n"
            "if os.name != 'nt': signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "Path('child-ready').touch()\ntime.sleep(3)\nPath('escaped-child').touch()\n",
            encoding='utf-8')
        command = self.command(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-B', 'descendant.py'])\ntime.sleep(30)\n")
        result = self.start(command, timeout=2)
        self.assertEqual(('accepted', 'timeout'), (result['status'], result['outcome']))
        self.assertTrue((self.command_cwd / 'child-ready').exists())
        time.sleep(3.2)
        self.assertFalse((self.command_cwd / 'escaped-child').exists())

    def test_successful_parent_cannot_leave_an_owned_background_descendant(self):
        child = self.command_cwd / 'descendant.py'
        child.write_text("import time\nfrom pathlib import Path\nPath('child-ready').touch()\n"
                         "time.sleep(3)\nPath('escaped-child').touch()\n", encoding='utf-8')
        command = self.command(
            "import subprocess, sys, time\nfrom pathlib import Path\n"
            "subprocess.Popen([sys.executable, '-B', 'descendant.py'])\n"
            "deadline = time.monotonic() + 5\n"
            "while not Path('child-ready').exists() and time.monotonic() < deadline: time.sleep(.01)\n"
            "sys.exit(0 if Path('child-ready').exists() else 2)\n")
        result = self.start(command, timeout=8)
        self.assertEqual(('accepted', 'success'), (result['status'], result['outcome']))
        time.sleep(3.2)
        self.assertFalse((self.command_cwd / 'escaped-child').exists())

    def test_active_and_idle_use_only_tool_output_and_survive_new_user_input(self):
        command = self.command('print("complete")\n')
        for state in ('active', 'idle'):
            with self.subTest(state=state):
                self.server.thread['status'] = {'type': state}
                original = listener.run_command

                def run_and_update(request, path, environment=None):
                    result = original(request, path, environment)
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

        def run_and_replace(request, output, environment=None):
            result = original(request, output, environment)
            self.arguments.return_value = [str(self.root / 'replacement-codex'), 'app-server']
            return result

        with mock.patch.object(listener, 'run_command', side_effect=run_and_replace):
            result = self.start(self.command('print("done")\n'))
        self.assertEqual(('unavailable', 'success'), (result['status'], result['outcome']))
        self.assertEqual([], self.server.submissions())

    def test_target_change_before_callback_prevents_submission(self):
        original = listener.run_command

        def run_and_replace(request, output, environment=None):
            result = original(request, output, environment)
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
                                 listener.BackendIdentity.from_value(BACKEND), str(Path(sys.executable).resolve()))
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

    def test_running_native_version_must_match_the_schema_executable(self):
        target = listener.Target(THREAD, str(self.profile), str(self.thread_cwd),
                                 listener.BackendIdentity.from_value(BACKEND), sys.executable)
        native = mock.Mock(initialization={'userAgent': 'listener/0.163.0-alpha.1 (Windows)',
                                          'codexHome': str(self.profile)})
        with mock.patch.object(listener.subprocess, 'run', return_value=mock.Mock(
                stdout='codex-cli 0.163.0-alpha.1\n')) as process:
            self.verify_running_version(native, target)
            process.return_value.stdout = 'codex-cli 0.164.0\n'
            with self.assertRaisesRegex(RuntimeError, 'versions differ'):
                self.verify_running_version(native, target)
            process.return_value.stdout = 'codex-cli 0.163.0-alpha.1\n'
            native.initialization['codexHome'] = str(self.root / 'foreign-profile')
            with self.assertRaisesRegex(RuntimeError, 'version or profile'):
                self.verify_running_version(native, target)

    def test_running_version_change_during_schema_probe_prevents_command(self):
        self.version.side_effect = [None, RuntimeError('Native daemon and executable versions differ')]
        with self.assertRaisesRegex(RuntimeError, 'versions differ'):
            self.start(self.command('print("never")\n'))
        self.launch.assert_not_called()

    def test_windows_command_line_reader_checks_lengths_encoding_and_complete_reads(self):
        text = '"C:\\native path\\codex.exe" app-server "café 雪"'
        raw = text.encode('utf-16-le')

        def descriptor(length, maximum, address=4096):
            return length.to_bytes(2, 'little') + maximum.to_bytes(2, 'little') + bytes(4) + address.to_bytes(8, 'little')

        read = mock.Mock(side_effect=[descriptor(len(raw), len(raw) + 2), raw])
        self.assertEqual(text, listener.client.windows_command_line(read, 8192))
        self.assertEqual([mock.call(8192 + 0x70, 16), mock.call(4096, len(raw))], read.call_args_list)
        for length, maximum, address in ((0, 2, 4096), (3, 4, 4096), (4, 3, 4096),
                                         (4, 65535, 4096), (65535, 65535, 4096), (4, 4, 0)):
            with self.subTest(length=length, maximum=maximum, address=address):
                with self.assertRaisesRegex(RuntimeError, 'length or address'):
                    listener.client.windows_command_line(mock.Mock(return_value=descriptor(length, maximum, address)), 8192)
        with self.assertRaisesRegex(RuntimeError, 'metadata is incomplete'):
            listener.client.windows_command_line(mock.Mock(return_value=bytes(15)), 8192)
        with self.assertRaisesRegex(RuntimeError, 'command line is incomplete'):
            listener.client.windows_command_line(mock.Mock(side_effect=[descriptor(4, 4), bytes(2)]), 8192)
        with self.assertRaises(UnicodeDecodeError):
            listener.client.windows_command_line(mock.Mock(side_effect=[descriptor(2, 2), b'\x00\xd8']), 8192)
        with self.assertRaisesRegex(RuntimeError, 'embedded terminator'):
            listener.client.windows_command_line(mock.Mock(side_effect=[descriptor(2, 2), bytes(2)]), 8192)

    @unittest.skipIf(os.name == 'nt', 'Linux verifies the mapped image through /proc; Windows uses native version')
    def test_linux_mapped_image_must_be_the_same_file_as_the_schema_executable(self):
        self.verify_mapped_image(os.getpid(), sys.executable)
        replacement = self.root / 'replacement-python'
        shutil.copyfile(sys.executable, replacement)
        with self.assertRaisesRegex(RuntimeError, 'replaced on disk'):
            self.verify_mapped_image(os.getpid(), str(replacement))

    def test_transient_sharing_failures_preserve_uncertainty_without_replay(self):
        original_replace = os.replace
        original_read = Path.read_text
        replacements = 3
        reads = 2

        def sharing_error():
            error = PermissionError(13, 'A concurrent status reader holds the state')
            error.winerror = 32
            return error

        def replace(source, destination):
            nonlocal replacements
            if Path(destination).name == 'state.json' and replacements:
                replacements -= 1
                raise sharing_error()
            return original_replace(source, destination)

        def read(path, *args, **kwargs):
            nonlocal reads
            if path == self.job() / 'state.json' and reads:
                reads -= 1
                raise sharing_error()
            return original_read(path, *args, **kwargs)

        self.server.submission_error = RuntimeError('Disconnected after submission')
        command = self.command('print("finished")\n')
        with mock.patch.object(listener.os, 'replace', side_effect=replace), mock.patch.object(
                Path, 'read_text', read):
            result = self.start(command)
            self.assertEqual('delivery-unknown', result['status'])
            self.assertEqual('delivery-unknown', self.start(command)['status'])
            self.assertEqual('delivery-unknown', listener.worker(self.job())['status'])
        self.assertEqual((0, 0), (reads, replacements))
        self.assertEqual(1, len(self.server.submissions()))
        self.assertEqual(1, self.launch.call_count)

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics; portable retry/no-replay coverage runs above')
    def test_atomic_state_replacement_waits_for_a_real_windows_reader(self):
        import ctypes

        kernel, _security = listener_windows.api()
        path = self.root / 'held-state.json'
        listener.write_json(path, {'status': 'running'})
        handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0, None)
        self.assertNotEqual(ctypes.c_void_p(-1).value, handle)
        release = threading.Thread(target=lambda: (time.sleep(.15), kernel.CloseHandle(handle)))
        release.start()
        try:
            listener.write_json(path, {'status': 'accepted'})
        finally:
            release.join(2)
        self.assertEqual('accepted', listener.read_json(path)['status'])

    @unittest.skipUnless(os.name == 'nt', 'Task Scheduler environment transfer is Windows-only')
    def test_windows_environment_transfer_is_private_bounded_and_memory_only(self):
        result = queue.Queue()
        environment = {'GH_TOKEN': 'private-test-token-雪', 'PATH': str(self.command_cwd),
                       'PYTHONPATH': 'caller-module-path'}
        with listener_windows.EnvironmentPipe() as pipe:
            worker = threading.Thread(target=lambda: result.put(listener_windows.receive_environment(pipe.name)))
            worker.start()
            pipe.send(environment)
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(environment, result.get_nowait())
        self.assertEqual([], list(self.root.rglob('*private-test-token*')))
        with self.assertRaisesRegex(RuntimeError, 'local private'):
            listener_windows.receive_environment('\\\\remote\\pipe\\agent-listener-' + 'a' * 48)
        with listener_windows.EnvironmentPipe() as pipe:
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                pipe.send(environment, timeout=.05)

    @unittest.skipUnless(os.name == 'nt', 'Documented Windows process identity checks')
    def test_windows_daemon_identity_checks_live_start_image_and_profile_without_cim(self):
        import ctypes
        from ctypes import wintypes

        kernel, _security = listener_windows.api()

        def identity(pid):
            process = listener_windows.checked(kernel.OpenProcess(0x1000, False, pid))
            try:
                creation, exited, system, user = (wintypes.FILETIME() for _ in range(4))
                listener_windows.checked(kernel.GetProcessTimes(process, ctypes.byref(creation), ctypes.byref(exited),
                                                                 ctypes.byref(system), ctypes.byref(user)))
                return {'pid': pid,
                        'process_start_time': str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)}
            finally:
                kernel.CloseHandle(process)

        script = self.root / 'app-server'
        script.write_text("import time\nfrom pathlib import Path\nPath('role-ready').touch()\ntime.sleep(30)\n",
                          encoding='utf-8')
        environment = dict(os.environ, **{listener.client.RECORD_ENV: '', listener.client.RECORD_PATH_ENV: ''})
        process = subprocess.Popen([sys.executable, 'app-server', 'two words', 'café 雪'], cwd=self.root,
                                   env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(lambda: (process.terminate(), process.wait(timeout=5)) if process.poll() is None else None)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (self.root / 'role-ready').exists():
            time.sleep(.01)
        self.assertTrue((self.root / 'role-ready').exists())
        backend = identity(process.pid)
        path = self.profile / 'app-server-daemon' / 'daemon.pid'
        path.parent.mkdir()
        native = {'pid': backend['pid'], 'processStartTime': backend['process_start_time']}
        path.write_text(json.dumps(native), encoding='utf-8')
        with mock.patch.dict(os.environ, {listener.client.RECORD_ENV: '', listener.client.RECORD_PATH_ENV: ''}):
            executable = self.windows_daemon_executable(str(self.profile), listener.BackendIdentity.from_value(backend))
            self.assertEqual(listener.path_key(sys.executable), listener.path_key(executable))
            self.arguments.assert_not_called()
            context = listener.client.process_context(process.pid, include_command_line=True)
            self.assertEqual(['two words', 'café 雪'], context['arguments'][-2:])
            own = identity(os.getpid())
            path.write_text(json.dumps({'pid': own['pid'], 'processStartTime': own['process_start_time']}),
                            encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'not an app-server daemon'):
                self.windows_daemon_executable(str(self.profile), listener.BackendIdentity.from_value(own))
            path.write_text(json.dumps(native), encoding='utf-8')
            stale = {**backend, 'process_start_time': str(int(backend['process_start_time']) + 1)}
            path.write_text(json.dumps({**native, 'processStartTime': stale['process_start_time']}),
                            encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'stale or reused'):
                self.windows_daemon_executable(str(self.profile), listener.BackendIdentity.from_value(stale))
            path.write_text(json.dumps(native), encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'metadata changed'):
                self.windows_daemon_executable(str(self.profile), listener.BackendIdentity.from_value(
                    {**backend, 'pid': backend['pid'] + 1}))
            other_profile = self.root / 'foreign-profile'
            other_path = other_profile / 'app-server-daemon' / 'daemon.pid'
            other_path.parent.mkdir(parents=True)
            other_path.write_text(json.dumps(native), encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'profile or recording'):
                self.windows_daemon_executable(str(other_profile), listener.BackendIdentity.from_value(backend))
            with mock.patch.object(listener_windows, 'daemon_executable',
                                   side_effect=self.windows_daemon_executable):
                target = listener.Target(THREAD, str(self.profile), str(self.thread_cwd),
                                         listener.BackendIdentity.from_value(backend),
                                         str(self.root / 'different-native-executable'))
                with self.assertRaisesRegex(RuntimeError, 'daemon or executable changed'):
                    listener.verify_backend(target)

    def test_command_environment_is_explicit_and_never_persisted(self):
        command = self.command("import os, sys\nsys.exit(0 if os.environ.get('CALLER_SECRET') "
                               "== 'temporary-test-value' else 2)\n")
        environment = dict(os.environ, CALLER_SECRET='temporary-test-value')
        self.launch.side_effect = lambda job: listener.worker(job, environment)
        result = self.start(command)
        self.assertEqual(('accepted', 'success'), (result['status'], result['outcome']))
        for path in self.job().iterdir():
            self.assertNotIn('temporary-test-value', path.read_text(encoding='utf-8'))



if __name__ == '__main__':
    unittest.main()
