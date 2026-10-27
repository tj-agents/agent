import argparse
import hashlib
import importlib.util
import json
import ntpath
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid


class FollowupError(Exception):
    pass


class Rejected(FollowupError):
    pass


class ReceiptMismatch(FollowupError):
    pass


def _agent_store_reader():
    """Load the Store reader only from this package's nearest verified root."""
    for root in Path(__file__).resolve().parents:
        runtime = root / ".agents" / "agent" / "runtime"
        locator = runtime / "package_root.py"
        if not locator.is_file():
            continue
        spec = importlib.util.spec_from_file_location("followup_package_root", locator)
        package_root = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package_root)
        if not package_root.is_package_root(root):
            continue
        path = root / ".agents" / "agent-store" / "runtime" / "reader.py"
        if not path.is_file():
            break
        spec = importlib.util.spec_from_file_location("followup_store_reader", path)
        reader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reader)
        return reader
    raise FollowupError("The shipped Agent Store reader is unavailable; no send attempted")


def responsibility(receipt):
    reader = _agent_store_reader()
    snapshot = reader.ReadOnlyWorkItemStore().inspect(execution_key=receipt["routing"]["execution_key"])
    if snapshot["work_items"][0]["status"] == "done":
        raise FollowupError("Recipient execution is complete; reconcile responsibility before sending")
    matches = [row for row in snapshot["sessions"]
               if row["handle"] == "codex:" + receipt["thread_id"] and row["ended_at"] is None
               and (row["role"] == "owner" or (row["role"] or "").startswith("bounded: "))
               and path_key(row["cwd"]) == receipt["cwd"]
               and path_key(row.get("profile") or "") == receipt["codex_home"]]
    if len(matches) != 1:
        raise FollowupError("Recipient has no verified responsibility for this execution; no send attempted")
    return matches[0]["role"]


def admit_route(proxy, receipt):
    route = receipt.get("routing") or {}
    if not isinstance(route.get("reason"), str) or not route["reason"].strip():
        raise FollowupError("State the concrete reason this recipient needs the message")
    if route.get("execution_key"):
        responsibility(receipt)
        return
    original_path = route.get("corrects")
    if not original_path:
        raise FollowupError("Resolve recipient responsibility or identify the mistaken delivery receipt")
    original = json.loads(Path(original_path).read_text(encoding="utf-8"))
    validate_receipt(original)
    if any(original[key] != receipt[key] for key in ("thread_id", "cwd", "codex_home")):
        raise FollowupError("Correction must address the original recipient and profile")
    if original["client_id"] == receipt["client_id"] or delivery(proxy, original) is None:
        raise FollowupError("Original mistaken message delivery is unverified; no correction sent")


def path_key(value):
    value = str(value)
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    if ntpath.isabs(value) and (ntpath.splitdrive(value)[0] or '\\' in value):
        return ntpath.normcase(ntpath.normpath(value))
    return os.path.normcase(os.path.abspath(value))


class Proxy:
    def __init__(self, executable, profile, deadline):
        self.deadline = deadline
        env = dict(os.environ, CODEX_HOME=str(profile))
        for root in Path(__file__).resolve().parents:
            markers = (root / '.git', root / '.codex-plugin' / 'plugin.json', root / '.claude-plugin' / 'plugin.json')
            if any(marker.exists() for marker in markers):
                path = root / '.agents' / 'agent' / 'runtime' / 'codex' / 'hook_trust.py'
                if not path.is_file():
                    break
                if str(path.parents[1]) not in sys.path:
                    sys.path.insert(0, str(path.parents[1]))
                spec = importlib.util.spec_from_file_location('codex_followup_transport', path)
                transport = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(transport)
                self.server = transport.AppServer(executable, Path.cwd(), environment=env,
                                                  timeout=max(0.1, deadline - time.monotonic()), existing=True)
                self.process = self.server.process
                return
        raise FollowupError('The packaged Codex WebSocket transport was not found')

    def request(self, method, params):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise FollowupError('Timed out; delivery has not been verified')
        completed = queue.Queue(maxsize=1)

        def run_request():
            try:
                completed.put((True, self.server.request(method, params)))
            except (BrokenPipeError, OSError, RuntimeError, ValueError) as error:
                completed.put((False, error))

        self.server.timeout = remaining
        worker = threading.Thread(target=run_request, daemon=True)
        worker.start()
        try:
            succeeded, value = completed.get(timeout=remaining)
        except queue.Empty as error:
            self.close()
            worker.join(timeout=0.1)
            raise FollowupError('timed out; delivery has not been verified') from error
        if not succeeded:
            raise FollowupError(str(value)) from value
        result = value
        if not isinstance(result, dict):
            raise FollowupError(f'{method} returned an unsupported response')
        return result

    def initialize(self):
        try:
            self.server.__enter__()
        except RuntimeError as error:
            raise FollowupError(str(error)) from error

    def close(self):
        try:
            self.server.close()
        except (BrokenPipeError, OSError, ValueError):
            pass


def pages(proxy, method, params):
    cursor = None
    seen = set()
    while True:
        result = proxy.request(method, dict(params, cursor=cursor, limit=100))
        if not isinstance(result.get('data'), list):
            raise FollowupError(f'{method} returned an unsupported page')
        yield from result['data']
        cursor = result.get('nextCursor')
        if not cursor:
            return
        if cursor in seen:
            raise FollowupError(f'{method} repeated its pagination cursor')
        seen.add(cursor)


def read_thread(proxy, receipt):
    thread = proxy.request('thread/read', {'threadId': receipt['thread_id']})['thread']
    if thread['id'] != receipt['thread_id'] or path_key(thread['cwd']) != receipt['cwd']:
        raise FollowupError('Recipient thread ID or cwd does not match; refusing to send')
    return thread


def delivery(proxy, receipt):
    read_thread(proxy, receipt)
    for entry in pages(proxy, 'thread/items/list', {
        'threadId': receipt['thread_id'], 'sortDirection': 'desc',
    }):
        item = entry['item']
        if item.get('type') == 'userMessage' and item.get('clientId') == receipt['client_id']:
            content = item.get('content', [])
            if (len(content) != 1 or content[0].get('type') != 'text'
                    or hashlib.sha256(content[0]['text'].encode('utf-8')).hexdigest()
                    != receipt['message_sha256']):
                raise FollowupError('Recipient client ID matches a different message')
            return {'status': 'delivered', 'thread_id': receipt['thread_id'],
                    'client_id': receipt['client_id'], 'turn_id': entry['turnId'],
                    'item_id': item['id'], 'acknowledged': False}
    return None


def wait_for_delivery(proxy, receipt, accepted):
    while True:
        result = delivery(proxy, receipt)
        if result:
            return result
        remaining = proxy.deadline - time.monotonic()
        if remaining <= 0.5:
            return {'status': 'accepted' if accepted else 'unverified',
                    'thread_id': receipt['thread_id'], 'client_id': receipt['client_id'],
                    'detail': 'No matching recipient user message observed; do not resend.',
                    'acknowledged': False}
        time.sleep(min(0.5, remaining))


def send(proxy, receipt, receipt_path, message):
    thread = read_thread(proxy, receipt)
    if not any(value == receipt['thread_id'] for value in pages(proxy, 'thread/loaded/list', {})):
        raise FollowupError('Target is not loaded in this server; no replacement session started')
    if thread.get('canAcceptDirectInput') is not True:
        raise FollowupError('Target does not advertise direct input; no message sent')
    state = thread['status']['type']
    params = {'threadId': receipt['thread_id'], 'clientUserMessageId': receipt['client_id'],
              'input': [{'type': 'text', 'text': message, 'text_elements': []}]}
    if state == 'active':
        active = [turn for turn in pages(proxy, 'thread/turns/list', {
            'threadId': receipt['thread_id'], 'sortDirection': 'desc', 'itemsView': 'notLoaded',
        }) if turn['status'] == 'inProgress']
        if len(active) != 1:
            raise FollowupError('Cannot identify exactly one active turn; no message sent')
        method = 'turn/steer'
        params['expectedTurnId'] = active[0]['id']
    elif state == 'idle':
        method = 'turn/start'
    else:
        raise FollowupError(f'Target runtime state {state!r} does not permit a follow-up')
    if not receipt_path.exists():
        admit_route(proxy, receipt)
    try:
        with receipt_path.open('x', encoding='utf-8') as output:
            json.dump(receipt, output, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError:
        existing = json.loads(receipt_path.read_text(encoding='utf-8'))
        validate_receipt(existing, receipt)
        return wait_for_delivery(proxy, existing, False)
    proxy.request(method, params)
    return wait_for_delivery(proxy, receipt, True)


def validate_receipt(receipt, expected=None):
    if receipt.get('version') != 1:
        raise FollowupError('Unsupported receipt version')
    for key in ('thread_id', 'client_id'):
        uuid.UUID(receipt[key])
    for key in ('cwd', 'codex_home', 'message_sha256'):
        if not isinstance(receipt.get(key), str) or not receipt[key]:
            raise FollowupError(f'Invalid receipt field: {key}')
    if expected and any(receipt[key] != expected[key] for key in (
        'thread_id', 'cwd', 'codex_home', 'message_sha256',
    )):
        raise ReceiptMismatch('Receipt belongs to a different target, profile or message')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Send to an existing Codex session and verify delivery')
    parser.add_argument('--codex', default='codex', help='Native Codex executable, not a shell command')
    parser.add_argument('--timeout', type=float, default=20)
    sub = parser.add_subparsers(dest='command', required=True)
    submit = sub.add_parser('send')
    submit.add_argument('--thread', required=True, type=uuid.UUID)
    submit.add_argument('--cwd', required=True)
    submit.add_argument('--codex-home', default=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    submit.add_argument('--message-file', required=True, type=Path)
    submit.add_argument('--receipt', required=True, type=Path)
    route = submit.add_mutually_exclusive_group(required=True)
    route.add_argument('--execution-key')
    route.add_argument('--corrects', type=Path)
    submit.add_argument('--reason', required=True)
    status = sub.add_parser('status')
    status.add_argument('--receipt', required=True, type=Path)
    args = parser.parse_args(argv)
    proxy = None
    receipt = None
    reserved = False
    try:
        if not 0 < args.timeout <= 60:
            raise FollowupError('--timeout must be greater than zero and at most 60 seconds')
        deadline = time.monotonic() + args.timeout
        exists = args.receipt.exists()
        if args.command == 'send':
            message = args.message_file.read_text(encoding='utf-8-sig')
            if not message.strip():
                raise FollowupError('Message file is empty')
            if not Path(args.cwd).is_dir() or not Path(args.codex_home).is_dir():
                raise FollowupError('Expected cwd and Codex profile must be existing directories')
            expected = {'version': 1, 'thread_id': str(args.thread), 'client_id': str(uuid.uuid4()),
                        'cwd': path_key(args.cwd), 'codex_home': path_key(args.codex_home),
                        'message_sha256': hashlib.sha256(message.encode('utf-8')).hexdigest()}
            expected['routing'] = {'reason': args.reason,
                                   **({'execution_key': args.execution_key} if args.execution_key
                                      else {'corrects': str(args.corrects.resolve())})}
            receipt = json.loads(args.receipt.read_text(encoding='utf-8')) if exists else expected
            validate_receipt(receipt, expected)
            if exists:
                reserved = True
            elif args.execution_key:
                if not args.reason.strip():
                    raise FollowupError('State the concrete reason this recipient needs the message')
                responsibility(receipt)
        else:
            receipt = json.loads(args.receipt.read_text(encoding='utf-8'))
            validate_receipt(receipt)
            reserved = True
        proxy = Proxy(args.codex, receipt['codex_home'], deadline)
        proxy.initialize()
        if args.command == 'status' or exists:
            result = wait_for_delivery(proxy, receipt, False)
        else:
            result = send(proxy, receipt, args.receipt, message)
    except (FollowupError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        mismatch = isinstance(error, ReceiptMismatch)
        if mismatch:
            status, next_action = 'unavailable', (
                'Nothing was sent for this message by this run; this receipt path already holds a '
                'receipt for a different message or target. Use a new receipt file for this message; '
                'do not paste it blindly, since a different send may already exist under the existing '
                'receipt.'
            )
        elif reserved or (args.command == 'send' and args.receipt.exists()):
            status, next_action = 'unverified', 'Check this receipt; do not resend.'
        elif args.command == 'status':
            status, next_action = 'unavailable', (
                'No valid receipt could be read for this thread. Delivery is unknown; check the target '
                'session directly before resending anything.'
            )
        else:
            status, next_action = 'unavailable', (
                'No send was attempted. Preserve task-specific authority evidence and the authorized scope, '
                'limits, holds, and remaining gates in the private recovery context and any required tool '
                'approval justification. Assess available supported automated coordination through '
                'agent:open for the verified exact owner; verify the exact native id, cwd, and Codex '
                'profile. If automated coordination is unavailable or ineligible, report the blocker and '
                'provide the message for the user to paste.'
            )
        if isinstance(error, Rejected):
            status = 'rejected'
        result = {'status': status, 'detail': str(error), 'receipt': str(args.receipt),
                  'acknowledged': False, 'next_action': next_action}
    finally:
        if proxy:
            proxy.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'delivered' else 2


if __name__ == '__main__':
    raise SystemExit(main())
