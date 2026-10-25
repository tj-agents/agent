import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'codex_followup', ROOT / '.agents/agent/utility/message/scripts/codex_followup.py'
)
FOLLOWUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FOLLOWUP)
THREAD = '00000000-0000-4000-8000-000000000001'
CLIENT = '00000000-0000-4000-8000-000000000002'


class Session:
    def __init__(self, receipt, state='active', delivered=True):
        self.deadline = time.monotonic() + 0.2
        self.receipt = receipt
        self.calls = []
        self.thread = {'id': THREAD, 'cwd': receipt['cwd'], 'status': {'type': state},
                       'canAcceptDirectInput': True}
        self.loaded = [THREAD]
        self.delivered = delivered
        self.submitted = False
        self.item_client = CLIENT
        self.fail_submit = False
        self.item_pages = False
        self.turns = [{'id': 'active-turn', 'status': 'inProgress'}]

    def request(self, method, params):
        self.calls.append((method, params))
        if method == 'thread/read':
            return {'thread': self.thread}
        if method == 'thread/loaded/list':
            return {'data': self.loaded}
        if method == 'thread/turns/list':
            return {'data': self.turns}
        if method in ('turn/steer', 'turn/start'):
            self.submitted = True
            self.item_client = params['clientUserMessageId']
            if self.fail_submit:
                raise FOLLOWUP.FollowupError('Disconnected after submission')
            return {'turnId': 'active-turn'}
        if method == 'thread/items/list':
            if self.item_pages and not params.get('cursor'):
                return {'data': [{'turnId': 'x', 'item': {'type': 'agentMessage',
                         'id': 'agent', 'text': CLIENT}}], 'nextCursor': 'next'}
            data = [{'turnId': 'active-turn', 'item': {'type': 'userMessage',
                     'clientId': self.item_client, 'id': 'received',
                     'content': [{'type': 'text', 'text': 'exact message'}]}}]
            return {'data': data if self.delivered else []}
        raise AssertionError(method)

    def initialize(self):
        pass

    def close(self):
        pass


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='followup tests ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.receipt_path = self.root / 'receipt.json'
        self.message = self.root / 'message.txt'
        self.message.write_text('exact message', encoding='utf-8')
        self.receipt = {'version': 1, 'thread_id': THREAD, 'client_id': CLIENT,
                        'cwd': FOLLOWUP.path_key(self.root),
                        'codex_home': FOLLOWUP.path_key(self.root),
                        'message_sha256': hashlib.sha256(b'exact message').hexdigest()}

    def submit(self, proxy):
        return FOLLOWUP.send(proxy, self.receipt, self.receipt_path, 'exact message')

    def test_active_session_steers_exact_turn_and_verifies_client_id(self):
        proxy = Session(self.receipt)
        result = self.submit(proxy)
        self.assertEqual('delivered', result['status'])
        self.assertFalse(result['acknowledged'])
        mutation = [call for call in proxy.calls if call[0].startswith('turn/')]
        self.assertEqual([('turn/steer', {
            'threadId': THREAD, 'clientUserMessageId': CLIENT,
            'expectedTurnId': 'active-turn',
            'input': [{'type': 'text', 'text': 'exact message', 'text_elements': []}],
        })], mutation)
        self.assertEqual(self.receipt, json.loads(self.receipt_path.read_text()))

    def test_idle_session_uses_same_loaded_thread_without_overrides(self):
        proxy = Session(self.receipt, state='idle')
        self.assertEqual('delivered', self.submit(proxy)['status'])
        mutations = [call for call in proxy.calls if call[0].startswith('turn/')]
        self.assertEqual('turn/start', mutations[0][0])
        self.assertEqual({'threadId', 'clientUserMessageId', 'input'}, set(mutations[0][1]))
        self.assertFalse(any(call[0] in ('thread/start', 'thread/resume') for call in proxy.calls))

    def test_queue_acceptance_without_recipient_message_is_not_delivery(self):
        result = self.submit(Session(self.receipt, delivered=False))
        self.assertEqual('accepted', result['status'])
        self.assertFalse(result['acknowledged'])

    def test_wrong_thread_or_cwd_prevents_submission(self):
        for field, value in [('id', CLIENT), ('cwd', str(self.root / 'different'))]:
            with self.subTest(field=field):
                proxy = Session(self.receipt)
                proxy.thread[field] = value
                with self.assertRaisesRegex(FOLLOWUP.FollowupError, 'does not match'):
                    self.submit(proxy)
                self.assertFalse(proxy.submitted)
                self.assertFalse(self.receipt_path.exists())

    def test_unloaded_or_unsupported_recipient_cannot_create_an_owner(self):
        for variant in ('unloaded', 'unsupported', 'missing-capability', 'bad-status'):
            with self.subTest(variant=variant):
                proxy = Session(self.receipt)
                if variant == 'unloaded':
                    proxy.loaded = []
                elif variant == 'unsupported':
                    proxy.thread['canAcceptDirectInput'] = False
                elif variant == 'missing-capability':
                    del proxy.thread['canAcceptDirectInput']
                else:
                    proxy.thread['status']['type'] = 'systemError'
                with self.assertRaises(FOLLOWUP.FollowupError):
                    self.submit(proxy)
                self.assertFalse(proxy.submitted)
                self.assertFalse(self.receipt_path.exists())

    def test_ambiguous_active_turn_is_rejected_before_receipt(self):
        proxy = Session(self.receipt)
        proxy.turns.append({'id': 'other-turn', 'status': 'inProgress'})
        with self.assertRaisesRegex(FOLLOWUP.FollowupError, 'exactly one'):
            self.submit(proxy)
        self.assertFalse(proxy.submitted)
        self.assertFalse(self.receipt_path.exists())

    def test_receipt_is_durable_before_transport_and_retry_never_resends(self):
        proxy = Session(self.receipt, delivered=False)
        proxy.fail_submit = True
        with self.assertRaisesRegex(FOLLOWUP.FollowupError, 'Disconnected'):
            self.submit(proxy)
        self.assertTrue(self.receipt_path.is_file())
        retry = Session(self.receipt, delivered=False)
        result = self.submit(retry)
        self.assertEqual('unverified', result['status'])
        self.assertFalse(retry.submitted)

    def test_foreign_receipt_prevents_a_concurrent_sender(self):
        foreign = dict(self.receipt, message_sha256='different')
        self.receipt_path.write_text(json.dumps(foreign))
        proxy = Session(self.receipt)
        with self.assertRaisesRegex(FOLLOWUP.FollowupError, 'different target'):
            self.submit(proxy)
        self.assertFalse(proxy.submitted)

    def test_pagination_finds_user_message_but_not_an_assistant_quote(self):
        proxy = Session(self.receipt)
        proxy.item_pages = True
        self.assertEqual('delivered', FOLLOWUP.delivery(proxy, self.receipt)['status'])
        proxy.item_client = 'someone-else'
        self.assertIsNone(FOLLOWUP.delivery(proxy, self.receipt))

    def test_matching_client_id_with_wrong_content_is_not_delivery(self):
        proxy = Session(self.receipt)
        receipt = dict(self.receipt, message_sha256='different')
        with self.assertRaisesRegex(FOLLOWUP.FollowupError, 'different message'):
            FOLLOWUP.delivery(proxy, receipt)

    def test_turn_race_rejection_never_falls_back_to_another_send(self):
        class RacingSession(Session):
            def request(self, method, params):
                if method == 'turn/steer':
                    self.calls.append((method, params))
                    raise FOLLOWUP.Rejected('Expected turn is no longer active')
                return super().request(method, params)
        proxy = RacingSession(self.receipt)
        code, result = self.run_main(self.arguments(), proxy)
        self.assertEqual(2, code)
        self.assertEqual('rejected', result['status'])
        self.assertEqual(1, sum(method.startswith('turn/') for method, _ in proxy.calls))
        self.assertTrue(self.receipt_path.exists())

    def test_cyclic_pagination_is_an_error(self):
        class Cyclic:
            def request(self, method, params):
                return {'data': [], 'nextCursor': 'again'}
        with self.assertRaisesRegex(FOLLOWUP.FollowupError, 'repeated'):
            list(FOLLOWUP.pages(Cyclic(), 'thread/items/list', {}))

    def test_windows_extended_paths_match(self):
        self.assertEqual(FOLLOWUP.path_key(r'\\?\C:\Work\Repo'),
                         FOLLOWUP.path_key('c:/work/repo/'))
        self.assertEqual(FOLLOWUP.path_key(r'\\?\UNC\Server\Share\Repo'),
                         FOLLOWUP.path_key(r'\\server\share\repo'))

    def run_main(self, arguments, proxy):
        output = io.StringIO()
        with patch.object(FOLLOWUP, 'Proxy', return_value=proxy), contextlib.redirect_stdout(output):
            code = FOLLOWUP.main(arguments)
        return code, json.loads(output.getvalue())

    def arguments(self):
        return ['--timeout', '0.1', 'send', '--thread', THREAD, '--cwd', str(self.root),
                '--codex-home', str(self.root), '--message-file', str(self.message),
                '--receipt', str(self.receipt_path)]

    def test_cli_retry_checks_history_without_writing_again(self):
        self.receipt_path.write_text(json.dumps(self.receipt))
        proxy = Session(self.receipt)
        code, result = self.run_main(self.arguments(), proxy)
        self.assertEqual(0, code)
        self.assertEqual('delivered', result['status'])
        self.assertFalse(proxy.submitted)

    def test_status_is_read_only(self):
        self.receipt_path.write_text(json.dumps(self.receipt))
        original = self.receipt_path.read_bytes()
        proxy = Session(self.receipt, delivered=False)
        code, result = self.run_main(['status', '--receipt', str(self.receipt_path)], proxy)
        self.assertEqual(2, code)
        self.assertEqual('unverified', result['status'])
        self.assertFalse(proxy.submitted)
        self.assertEqual(original, self.receipt_path.read_bytes())

    def test_unavailable_proxy_never_claims_sent(self):
        output = io.StringIO()
        with patch.object(FOLLOWUP, 'Proxy', side_effect=OSError('No proxy')), \
                patch.object(FOLLOWUP, 'send') as send, \
                contextlib.redirect_stdout(output):
            code = FOLLOWUP.main(self.arguments())
        result = json.loads(output.getvalue())
        self.assertEqual(2, code)
        self.assertEqual('unavailable', result['status'])
        self.assertLess(
            result['next_action'].index('task-specific authority evidence'),
            result['next_action'].index('codex:open'),
        )
        self.assertLess(
            result['next_action'].index('codex:open'),
            result['next_action'].index('provide the message'),
        )
        self.assertIn('unavailable or ineligible', result['next_action'])
        self.assertIn('blocker', result['next_action'])
        self.assertFalse(result['acknowledged'])
        self.assertFalse(self.receipt_path.exists())
        send.assert_not_called()

    def test_message_change_with_same_receipt_is_rejected(self):
        self.receipt_path.write_text(json.dumps(self.receipt))
        self.message.write_text('changed', encoding='utf-8')
        proxy = Session(self.receipt)
        code, result = self.run_main(self.arguments(), proxy)
        self.assertEqual(2, code)
        self.assertIn('different target', result['detail'])
        self.assertEqual([], proxy.calls)

    def test_rejection_before_any_receipt_requires_recovery_before_manual_fallback(self):
        class RejectingSession(Session):
            def initialize(self):
                raise FOLLOWUP.Rejected('unsupported')

        with patch.object(FOLLOWUP, 'send') as send:
            code, result = self.run_main(self.arguments(), RejectingSession(self.receipt))
        self.assertEqual(2, code)
        self.assertEqual('rejected', result['status'])
        self.assertLess(
            result['next_action'].index('task-specific authority evidence'),
            result['next_action'].index('codex:open'),
        )
        self.assertLess(
            result['next_action'].index('codex:open'),
            result['next_action'].index('provide the message'),
        )
        self.assertIn('unavailable or ineligible', result['next_action'])
        self.assertIn('blocker', result['next_action'])
        self.assertFalse(result['acknowledged'])
        self.assertFalse(self.receipt_path.exists())
        send.assert_not_called()

    def test_preexisting_mismatched_receipt_does_not_claim_unverified(self):
        foreign = dict(self.receipt, message_sha256='different')
        self.receipt_path.write_text(json.dumps(foreign))
        proxy = Session(self.receipt)
        code, result = self.run_main(self.arguments(), proxy)
        self.assertEqual(2, code)
        self.assertEqual('unavailable', result['status'])
        self.assertIn('Nothing was sent for this message', result['next_action'])
        self.assertNotIn('do not resend', result['next_action'])
        self.assertEqual([], proxy.calls)

    def test_status_with_missing_receipt_does_not_invite_blind_paste(self):
        missing = self.root / 'missing-receipt.json'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = FOLLOWUP.main(['status', '--receipt', str(missing)])
        result = json.loads(output.getvalue())
        self.assertEqual(2, code)
        self.assertEqual('unavailable', result['status'])
        self.assertNotIn('paste', result['next_action'])
        self.assertIn('check the target', result['next_action'].lower())

    def test_status_disconnect_with_valid_receipt_stays_unverified(self):
        self.receipt_path.write_text(json.dumps(self.receipt))

        class DisconnectingSession(Session):
            def request(self, method, params):
                raise FOLLOWUP.FollowupError('Codex proxy disconnected')

        code, result = self.run_main(['status', '--receipt', str(self.receipt_path)],
                                      DisconnectingSession(self.receipt))
        self.assertEqual(2, code)
        self.assertEqual('unverified', result['status'])

    def test_non_string_receipt_field_still_prints_json_status(self):
        bad_receipt = dict(self.receipt, thread_id=12345)
        self.receipt_path.write_text(json.dumps(bad_receipt))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = FOLLOWUP.main(['status', '--receipt', str(self.receipt_path)])
        result = json.loads(output.getvalue())
        self.assertEqual(2, code)
        self.assertEqual('unavailable', result['status'])
        self.assertNotIn('paste', result['next_action'])

    def test_odd_server_item_content_still_prints_json_status(self):
        class OddSession(Session):
            def request(self, method, params):
                if method == 'thread/items/list':
                    return {'data': [{'turnId': 'active-turn', 'item': {
                        'type': 'userMessage', 'clientId': CLIENT, 'id': 'received',
                        'content': ['not-a-dict'],
                    }}]}
                return super().request(method, params)

        self.receipt_path.write_text(json.dumps(self.receipt))
        code, result = self.run_main(['status', '--receipt', str(self.receipt_path)],
                                      OddSession(self.receipt))
        self.assertEqual(2, code)
        self.assertEqual('unverified', result['status'])


class ProxyTests(unittest.TestCase):
    def create_proxy(self, source, timeout=3):
        temp = tempfile.TemporaryDirectory(prefix='followup proxy ')
        self.addCleanup(temp.cleanup)
        handler = Path(temp.name) / 'handler.py'
        handler.write_text(source, encoding='utf-8')
        script = Path(temp.name) / 'proxy.py'
        script.write_text('''import base64, hashlib, json, struct, subprocess, sys, threading
handler = subprocess.Popen([sys.executable, '-u', %r], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           text=True, encoding='utf-8')
def exact(n):
    b = bytearray()
    while len(b) < n:
        x = sys.stdin.buffer.read(n-len(b))
        if not x: raise SystemExit
        b.extend(x)
    return bytes(b)
def frame(value):
    payload=json.dumps(value).encode(); n=len(payload); h=bytes([129, n if n<126 else 126])
    if n>=126: h += struct.pack('!H', n)
    sys.stdout.buffer.write(h+payload); sys.stdout.buffer.flush()
def output():
    for line in handler.stdout:
        try: frame(json.loads(line))
        except ValueError: return
threading.Thread(target=output, daemon=True).start()
headers=bytearray()
while not headers.endswith(b'\\r\\n\\r\\n'): headers.extend(exact(1))
key=[line.split(b':',1)[1].strip() for line in headers.split(b'\\r\\n') if line.lower().startswith(b'sec-websocket-key:')][0]
accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
sys.stdout.buffer.write(b'HTTP/1.1 101 Switching Protocols\\r\\nUpgrade: websocket\\r\\nConnection: Upgrade\\r\\nSec-WebSocket-Accept: '+accept+b'\\r\\n\\r\\n'); sys.stdout.buffer.flush()
while True:
    a,b=exact(2); n=b&127
    if n==126: n=struct.unpack('!H',exact(2))[0]
    elif n==127: n=struct.unpack('!Q',exact(8))[0]
    mask=exact(4); payload=bytes(x^mask[i%%4] for i,x in enumerate(exact(n)))
    if (a&15)==8: break
    handler.stdin.write(payload.decode()+'\\n'); handler.stdin.flush()
''' % str(handler), encoding='utf-8')
        original = subprocess.Popen
        calls = []

        def launch(command, **kwargs):
            calls.append((command, kwargs['env']))
            return original([sys.executable, '-u', str(script)], **kwargs)

        with patch.object(FOLLOWUP.subprocess, 'Popen', side_effect=launch):
            proxy = FOLLOWUP.Proxy('codex-native', temp.name, time.monotonic() + timeout)
        self.addCleanup(proxy.close)
        self.assertEqual(['codex-native', 'app-server', 'proxy'], calls[0][0])
        self.assertEqual(temp.name, calls[0][1]['CODEX_HOME'])
        return proxy

    def test_real_stdio_framing_initialization_and_notifications(self):
        proxy = self.create_proxy('''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get('method') == 'initialize':
        print(json.dumps({'id': request['id'], 'result': {}}), flush=True)
    elif request.get('method') == 'probe':
        print(json.dumps({'method': 'notification', 'params': {}}), flush=True)
        print(json.dumps({'id': request['id'], 'result': {'probe': True}}), flush=True)
''')
        proxy.initialize()
        result = proxy.request('probe', {})
        self.assertTrue(result['probe'])
        proxy.close()
        self.assertIsNotNone(proxy.process.poll())

    def test_server_request_after_submission_gets_no_response_or_duplicate_turn(self):
        directory = tempfile.TemporaryDirectory(prefix='followup approval ')
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        message = root / 'message.txt'
        message.write_text('exact message', encoding='utf-8')
        receipt = root / 'receipt.json'
        thread = {'id': THREAD, 'cwd': str(root), 'status': {'type': 'idle'},
                  'canAcceptDirectInput': True}
        source = 'import json, sys\nthread = ' + repr(thread) + """
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialized':
        continue
    if method == 'turn/start':
        print(json.dumps({'id': 'approval', 'method': 'item/commandExecution/requestApproval', 'params': {}}), flush=True)
        continue
    result = {}
    if method == 'thread/read':
        result = {'thread': thread}
    elif method == 'thread/loaded/list':
        result = {'data': [thread['id']]}
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
"""
        proxy = self.create_proxy(source)
        proxy.deadline = time.monotonic() - 1

        def proxy_with_caller_deadline(executable, profile, deadline):
            proxy.deadline = deadline
            return proxy

        output = io.StringIO()
        with patch.object(FOLLOWUP, 'Proxy', side_effect=proxy_with_caller_deadline), \
                patch.object(proxy, '_write', wraps=proxy._write) as write, \
                contextlib.redirect_stdout(output):
            code = FOLLOWUP.main(['send', '--thread', THREAD, '--cwd', str(root),
                                  '--codex-home', str(root), '--message-file', str(message),
                                  '--receipt', str(receipt)])
        self.assertEqual(2, code)
        self.assertEqual('unverified', json.loads(output.getvalue())['status'])
        self.assertTrue(receipt.is_file())
        emitted = [call.args[0] for call in write.call_args_list]
        self.assertFalse(any(item.get('id') == 'approval' for item in emitted))
        self.assertLessEqual(sum(item.get('method', '').startswith('turn/') for item in emitted), 1)
        self.assertIsNotNone(proxy.process.poll())

    def test_disconnect_error_is_not_delivery(self):
        proxy = self.create_proxy('pass')
        with self.assertRaises(FOLLOWUP.FollowupError):
            proxy.initialize()

    def test_unresponsive_existing_proxy_is_bounded_and_cleaned_up(self):
        proxy = self.create_proxy('import time; time.sleep(30)', timeout=0.1)
        started = time.monotonic()
        with self.assertRaisesRegex(FOLLOWUP.FollowupError, 'timed out'):
            proxy.initialize()
        proxy.close()
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(proxy.process.poll())

    def test_large_write_to_nonreading_proxy_obeys_deadline(self):
        proxy = self.create_proxy('import time; time.sleep(30)', timeout=0.2)
        started = time.monotonic()
        with self.assertRaisesRegex(FOLLOWUP.FollowupError, 'timed out'):
            proxy.request('turn/start', {'input': [{'type': 'text', 'text': 'x' * 1000000}]})
        proxy.close()
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(proxy.process.poll())
        if proxy.writer:
            self.assertFalse(proxy.writer.is_alive())

    def test_expired_deadline_does_not_write_request(self):
        proxy = self.create_proxy('import time; time.sleep(30)')
        proxy.deadline = time.monotonic() - 1
        with patch.object(proxy, '_write') as write:
            with self.assertRaises(FOLLOWUP.FollowupError):
                proxy.request('turn/start', {})
        write.assert_not_called()

    def test_close_is_best_effort_when_stdin_close_raises(self):
        proxy = self.create_proxy('import time; time.sleep(30)', timeout=0.2)
        with patch.object(proxy.process.stdin, 'close', side_effect=BrokenPipeError('broken')):
            proxy.close()
        self.assertIsNotNone(proxy.process.poll())

    def test_close_failure_does_not_suppress_the_printed_result(self):
        directory = tempfile.TemporaryDirectory(prefix='followup close ')
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        message = root / 'message.txt'
        message.write_text('exact message', encoding='utf-8')
        receipt = root / 'receipt.json'
        proxy = self.create_proxy('import time; time.sleep(30)', timeout=0.2)
        output = io.StringIO()
        with patch.object(FOLLOWUP, 'Proxy', return_value=proxy), \
                patch.object(proxy.process.stdin, 'close', side_effect=BrokenPipeError('broken')), \
                contextlib.redirect_stdout(output):
            code = FOLLOWUP.main(['send', '--thread', THREAD, '--cwd', str(root),
                                  '--codex-home', str(root), '--message-file', str(message),
                                  '--receipt', str(receipt)])
        result = json.loads(output.getvalue())
        self.assertEqual(2, code)
        self.assertIn(result['status'], ('unverified', 'unavailable'))

    def test_rpc_rejection_and_malformed_output_fail_explicitly(self):
        for source in [
            "import json, sys; r=json.loads(sys.stdin.readline()); print(json.dumps({'id':r['id'],'error':{'code':-1,'message':'unsupported'}}),flush=True)",
            "print('not JSON',flush=True)",
        ]:
            with self.subTest(source=source):
                proxy = self.create_proxy(source)
                with self.assertRaises(FOLLOWUP.FollowupError):
                    proxy.initialize()


if __name__ == '__main__':
    unittest.main()
