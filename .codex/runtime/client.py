"""Bind managed Codex launches to their frontend process (Python 3.9+)."""

import errno
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import subprocess
import tempfile
import time
import sys

from codex import hook_trust
from host import Host

HOST = Host.CODEX
RECORD_ENV = "CODEX_TUI_RECORD_SESSION"
RECORD_PATH_ENV = "CODEX_TUI_SESSION_LOG_PATH"
TOKEN = re.compile(r"[0-9a-f]{64}\Z")
OTHER_COMMANDS = {
    "exec", "e", "review", "login", "logout", "mcp", "plugin", "app-server", "remote-control", "app",
    "completion", "update", "doctor", "sandbox", "debug", "apply", "a", "queue", "archive",
    "delete", "migrate-rollouts", "migrate-nfs-db", "unarchive", "cloud", "exec-server", "features", "help",
}
VALUE_OPTIONS = {
    "-c", "--config", "-i", "--image", "-m", "--model", "-p", "--profile", "-s", "--sandbox",
    "-a", "--ask-for-approval", "-C", "--cd", "--add-dir", "--enable", "--disable",
    "--remote", "--remote-auth-token-env", "--local-provider",
}
VIEW_CHANGES = {
    'AgentsOverviewWorktreeCreated', 'BeginThreadSwitchHistoryReplayBuffer', 'ChangeWorkingDirectory',
    'ClearUiAndSubmitUserMessage', 'FinishPromptRevert', 'ForkAgentsOverviewThread',
    'ForkAgentsOverviewThreadReady', 'ForkCurrentSession', 'ManagedWorktreeCreated',
    'NewAgentsOverviewSession', 'OpenAgentsOverview', 'OpenResumePicker',
    'ResetTranscriptForThreadSwitch', 'ResetTranscriptForThreadSwitchPreservingScreen',
    'ResumeSessionByIdOrName', 'RetrySafetyBufferedTurn', 'RevertSessionForPromptEdit',
    'SelectAgentThread', 'SelectAgentsOverviewThread', 'StartManagedWorktree', 'StartSide',
}


def command_position(arguments):
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            return None
        option, separator, _value = value.partition("=")
        attached = len(value) > 2 and value[:2] in VALUE_OPTIONS
        if option in VALUE_OPTIONS:
            index += 1 if separator else 2
        elif attached or value.startswith("-"):
            index += 1
        else:
            return index
    return None


def command_name(arguments):
    index = command_position(arguments)
    return arguments[index] if index is not None else None


def is_session(arguments):
    options = arguments[:arguments.index("--")] if "--" in arguments else arguments
    return (not any(value in {"--help", "-h", "--version", "-V"} for value in options)
            and command_name(arguments) not in OTHER_COMMANDS)


def profile_directory(environ=None):
    values = os.environ if environ is None else environ
    if os.name == 'nt':
        values = {key.upper(): value for key, value in values.items()}
    return Path(values.get(HOST.home_variable) or Path.home() / HOST.home_directory).resolve()


def binding_path(token):
    if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
        raise RuntimeError("Invalid CLI client binding token")
    state = Path(os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state")
    return state / "cli-clients" / token / "client.json"


def daemon_identity(environment=None, expected_pid=None):
    profile = profile_directory(environment)
    try:
        record = json.loads((profile / 'app-server-daemon/daemon.pid').read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise RuntimeError('A managed native Codex daemon is required for frontend attribution') from error
    if (not isinstance(record, dict) or type(record.get('pid')) is not int or record['pid'] <= 0
            or not isinstance(record.get('processStartTime'), (str, int))
            or not str(record['processStartTime'])
            or (expected_pid is not None and record['pid'] != expected_pid)):
        raise RuntimeError('Native Codex daemon identity does not match this session')
    context = process_context(record['pid'])
    arguments = process_arguments(record['pid'])
    if (Path(context['profile']) != profile or not arguments
            or command_name(arguments[1:]) != 'app-server'):
        raise RuntimeError('Native Codex daemon metadata could not be verified')
    if context.get(RECORD_ENV) or context.get(RECORD_PATH_ENV):
        raise RuntimeError('The shared daemon inherited frontend recording state; preserve it and restart through the managed launcher after its work finishes')
    return {'pid': record['pid'], 'process_start_time': str(record['processStartTime'])}


def prepare(arguments, project, environment, executable=None):
    values = dict(environment)
    for key in tuple(values):
        compared = key.upper() if os.name == 'nt' else key
        if compared in (RECORD_ENV, RECORD_PATH_ENV):
            values.pop(key)
    arguments = list(arguments)
    if not is_session(arguments):
        return arguments, values, None
    if not executable:
        raise RuntimeError('Native Codex executable is required for a managed frontend')
    backend = None
    if '--no-daemon' not in arguments:
        with tempfile.TemporaryFile(mode='w+', encoding='utf-8', errors='replace') as diagnostic:
            result = subprocess.run([executable, 'app-server', 'daemon', 'start'], cwd=project, env=dict(values),
                                    stdin=subprocess.DEVNULL, stdout=diagnostic, stderr=diagnostic, timeout=45)
            if result.returncode:
                diagnostic.seek(0)
                detail = diagnostic.read().strip()[-1000:] or f'exit {result.returncode}'
                raise RuntimeError('Cannot start the native Codex daemon: ' + detail)
        backend = daemon_identity(values)
    token = secrets.token_hex(32)
    path = binding_path(token)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=False)
    values[RECORD_ENV] = '1'
    values[RECORD_PATH_ENV] = str(path.with_name('events.jsonl'))
    ticket = {
        'schema_version': 1, 'token': token, 'host': HOST.value, 'backend': backend,
        'profile': str(profile_directory(values)), 'launch_cwd': str(Path(project).resolve()),
        'created_at': time.time(), 'recording_path': values[RECORD_PATH_ENV],
        'title': values.get('AGENT_CLI_TAB_TITLE'),
        'terminal_environment': {key: values[key] for key in (
            'TMUX', 'TMUX_PANE', 'KITTY_WINDOW_ID', 'KITTY_LISTEN_ON', 'KONSOLE_DBUS_SERVICE',
            'KONSOLE_DBUS_SESSION', 'KONSOLE_DBUS_WINDOW', 'WT_SESSION', 'AGENT_WINDOWS_TERMINAL_TAB_ID',
        ) if key in values},
    }
    prepared = ['--config', 'shell_environment_policy.set.' + RECORD_ENV + '=""',
                '--config', 'shell_environment_policy.set.' + RECORD_PATH_ENV + '=""', *arguments]
    return prepared, values, ticket


def attach(ticket, pid):
    if ticket is None:
        return
    path = binding_path(ticket['token'])
    value = {**ticket, 'pid': pid, 'recorded_at': time.time()}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    ticket['_attached'] = True
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write('\n')


def release(ticket):
    if ticket is None:
        return
    path = binding_path(ticket['token'])
    if ticket.get('_attached'):
        path.unlink(missing_ok=True)
        path.with_name('events.jsonl').unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def latest_input(path):
    latest = None
    headers = 0
    with Path(path).open('r', encoding='utf-8') as stream:
        while True:
            line = stream.readline(8 * 1024 * 1024 + 1)
            if not line:
                break
            if len(line) > 8 * 1024 * 1024 or not line.endswith('\n'):
                raise RuntimeError('Native frontend recording is incomplete or oversized')
            row = json.loads(line)
            if not isinstance(row, dict):
                raise RuntimeError('Native frontend recording is invalid')
            if row.get('dir') == 'meta' and row.get('kind') == 'session_start':
                headers += 1
                latest = None
            elif row.get('dir') == 'meta' and row.get('kind') == 'session_end':
                latest = None
            elif row.get('dir') == 'to_tui' and (row.get('kind') in ('new_session', 'clear_ui')
                    or row.get('variant') in VIEW_CHANGES):
                latest = None
            elif row.get('dir') == 'from_tui' and row.get('kind') == 'op':
                payload = row.get('payload')
                turn = payload.get('UserTurn') if isinstance(payload, dict) else None
                if isinstance(turn, dict):
                    identity = turn.get('client_user_message_id')
                    latest = identity if isinstance(identity, str) and identity else None
    if headers != 1:
        raise RuntimeError('Native frontend recording has no unique startup header')
    return latest


def current_input(server, session_id):
    thread = server.request('thread/read', {'threadId': session_id, 'includeTurns': False})['thread']
    if isinstance(thread.get('source'), dict) or thread.get('threadSource') in ('agent', 'subagent'):
        raise RuntimeError('A background agent thread does not own a CLI frontend')
    turns = server.request('thread/turns/list', {'threadId': session_id, 'limit': 5,
                                               'sortDirection': 'desc', 'itemsView': 'notLoaded'})['data']
    for turn in turns:
        turn_id = turn.get('id')
        if not isinstance(turn_id, str) or not turn_id:
            raise RuntimeError('The latest native input has no attributable turn identity')
        cursor = None
        seen = set()
        for _ in range(128):
            params = {'threadId': session_id, 'turnId': turn_id, 'limit': 100, 'sortDirection': 'desc'}
            if cursor is not None:
                params['cursor'] = cursor
            page = server.request('thread/items/list', params)
            for value in page['data']:
                item = value['item']
                if item.get('type') == 'userMessage':
                    identity = item.get('clientId')
                    if not isinstance(identity, str) or not identity:
                        raise RuntimeError('The latest native input has no attributable frontend identity')
                    return {'turn_id': turn_id, 'client_message_id': identity,
                            'native_rollout': thread.get('path'), 'native_cwd': thread.get('cwd')}
            cursor = page.get('nextCursor')
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise RuntimeError('Native input pagination did not advance')
            seen.add(cursor)
        else:
            raise RuntimeError('Native input history exceeds the attribution scan limit')
    raise RuntimeError('This native thread has no attributable frontend input')


def peer_recording_path(peer, peer_state):
    try:
        return process_context(peer.pid).get(RECORD_PATH_ENV)
    except FileNotFoundError as error:
        disappearance = error
    except OSError as error:
        if error.errno != errno.ESRCH:
            raise
        disappearance = error
    state, current = peer_state(peer.pid)
    absent = state == 'absent'
    if state not in ('absent', 'present'):
        raise disappearance
    if absent:
        return None
    if current is not None and getattr(current, 'started_at', None) != getattr(peer, 'started_at', None):
        return None
    raise disappearance


def recorded_client(session_id, nearest, lookup, table, *, project=None, peer_state):
    if sys.version_info < (3, 11):
        raise RuntimeError('Codex frontend attribution requires Python 3.11 or newer')
    arguments = process_arguments(nearest.pid)
    private = bool(arguments and is_session(arguments[1:]) and '--no-daemon' in arguments)
    # `nearest` is the actual frontend ancestor. A shared daemon is a
    # separate process, so verify its identity independently and bind the
    # selected recorded frontend to both identities below.
    backend = None if private else daemon_identity()
    executable = arguments[0]
    with hook_trust.AppServer(executable, Path(project) if project is not None else Path.cwd(),
                             existing=not private, timeout=15) as server:
        submitted = current_input(server, session_id)
    state = Path(os.environ.get('AGENT_STATE_DIRECTORY') or Path.home() / '.agents-state')
    matches = []
    for path in (state / 'cli-clients').glob('*/client.json'):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(record, dict) or path != binding_path(record.get('token'))
                    or record.get('profile') != str(profile_directory()) or record.get('backend') != backend
                    or type(record.get('pid')) is not int or record['pid'] <= 0):
                continue
            info = lookup(record['pid'])
            if info is None or Path(info.name).stem.casefold() != HOST.value:
                continue
            times = [record.get(key) for key in ('created_at', 'recorded_at')]
            if (not all(type(value) in (int, float) and math.isfinite(value) and value > 0 for value in times)
                    or not times[0] <= times[1] or not times[0] - 2 <= info.started_at <= times[1] + 2):
                continue
            context = process_context(info.pid)
            event_path = str(path.with_name('events.jsonl'))
            if (record.get('recording_path') != event_path or context.get(RECORD_PATH_ENV) != event_path
                    or context.get(RECORD_ENV) not in ('1', 'true', 'TRUE', 'yes', 'YES')
                    or Path(context['profile']) != profile_directory()):
                continue
            arguments = process_arguments(info.pid)
            if (not arguments or not is_session(arguments[1:])
                    or ('--no-daemon' in arguments) != private
                    or latest_input(event_path) != submitted['client_message_id']):
                continue
            for other in table.values():
                if other.pid == info.pid or Path(other.name).stem.casefold() != HOST.value:
                    continue
                if peer_recording_path(other, peer_state) == event_path:
                    raise RuntimeError('Multiple native processes share a frontend recording; preserving every client')
            if (info.pid != nearest.pid
                    and (private or nearest.pid != backend['pid'])):
                continue
            matches.append((info, record))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    if len(matches) != 1:
        arguments = [HOST.value, '--cd', str(Path.cwd()), 'resume', session_id]
        if os.name == 'nt':
            command = ' '.join("'" + value.replace("'", "''") + "'" for value in arguments[1:])
            command = HOST.value + ' ' + command
        else:
            import shlex
            command = shlex.join(arguments)
        raise RuntimeError('Cannot attribute this input to one managed Codex frontend; preserve the shared backend. '
                           'Resume this conversation from a fresh managed terminal: ' + command)
    info, record = matches[0]
    return info, {**record, **submitted}


def comparable_path(path):
    """Compare equivalent Win32 drive/UNC spellings without changing paths used for I/O."""
    if not isinstance(path, PureWindowsPath):
        return path
    value = path.as_posix()
    if value[:8].casefold() == '//?/unc/':
        value = '//' + value[8:]
    elif re.match(r'//\?/[a-zA-Z]:/', value):
        value = value[4:]
    return PureWindowsPath(value)


class NativeCompletion:
    def __init__(self, entry, expected=None):
        required = ('native_rollout', 'profile', 'native_turn_id', 'client_message_id', 'session_id', 'recording_path')
        if not isinstance(entry, dict) or any(not isinstance(entry.get(key), str) or not entry[key] for key in required):
            raise RuntimeError('Native completion requires an exact frontend, profile and turn binding')
        self.entry = entry
        self.path = Path(entry['native_rollout'])
        sessions = comparable_path(Path(entry['profile']).resolve() / 'sessions')
        resolved = self.path.resolve(strict=True)
        self.resolved_path = comparable_path(resolved)
        if (not self.path.is_absolute() or self.resolved_path != comparable_path(self.path)
                or sessions not in self.resolved_path.parents):
            raise RuntimeError('Native rollout is outside the verified profile or has no bound turn')
        stat = self.path.stat()
        self.identity, self.minimum_size = [stat.st_dev, stat.st_ino], stat.st_size
        if expected is not None and (expected.get('identity') != self.identity
                                     or stat.st_size < expected.get('minimum_size', -1)):
            raise RuntimeError('Native rollout was replaced or truncated after arming')
        self.offset = 0
        self.metadata = self.started = self.input_seen = self.finished = False

    def pin(self):
        return {'identity': self.identity, 'minimum_size': self.minimum_size}

    def poll(self):
        if latest_input(self.entry['recording_path']) != self.entry['client_message_id']:
            raise RuntimeError('The frontend input changed after arming')
        stat = self.path.stat()
        if ([stat.st_dev, stat.st_ino] != self.identity or stat.st_size < max(self.offset, self.minimum_size)
                or comparable_path(self.path.resolve(strict=True)) != self.resolved_path):
            raise RuntimeError('Native rollout was replaced or truncated after arming')
        partial = False
        with self.path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if [opened.st_dev, opened.st_ino] != self.identity:
                raise RuntimeError('Native rollout changed while opening it')
            stream.seek(self.offset)
            while True:
                line = stream.readline(8 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 8 * 1024 * 1024:
                    raise RuntimeError('Native rollout record is oversized')
                if not line.endswith(b'\n'):
                    partial = True
                    break
                row = json.loads(line)
                self.offset = stream.tell()
                payload = row.get('payload') if isinstance(row, dict) else None
                if not isinstance(payload, dict):
                    raise RuntimeError('Native rollout record is invalid')
                if row.get('type') == 'session_meta':
                    if (self.metadata or payload.get('id') != self.entry['session_id']
                            or payload.get('source') not in ('cli', 'vscode')
                            or payload.get('thread_source', 'user') != 'user'):
                        raise RuntimeError('Native rollout does not belong to this CLI conversation')
                    self.metadata = True
                elif row.get('type') == 'event_msg':
                    kind, turn = payload.get('type'), payload.get('turn_id')
                    if kind == 'task_started':
                        if self.started:
                            raise RuntimeError('A new native turn started after arming')
                        self.started = turn == self.entry['native_turn_id']
                    elif self.started and kind == 'item_completed':
                        item = payload.get('item', {})
                        if item.get('type') == 'UserMessage':
                            if turn != self.entry['native_turn_id']:
                                raise RuntimeError('Native input did not belong to the bound turn')
                            identity = item.get('client_id')
                            if self.input_seen or not isinstance(identity, str) or not identity:
                                raise RuntimeError('New or unattributed native input invalidated closure')
                            self.input_seen = identity == self.entry['client_message_id']
                    elif self.started and kind in ('task_complete', 'turn_aborted'):
                        if turn != self.entry['native_turn_id'] or kind != 'task_complete' or self.finished:
                            raise RuntimeError('The bound native turn did not complete normally')
                        self.finished = True
        if not self.metadata:
            raise RuntimeError('Native rollout has no session identity')
        return self.finished and self.input_seen and not partial


def process_arguments(pid):
    if os.name != "nt":
        return [value.decode("utf-8", errors="surrogateescape")
                for value in (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0") if value]
    import ctypes
    from ctypes import wintypes

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
         "(Get-CimInstance Win32_Process -Filter 'ProcessId = %d').CommandLine | ConvertTo-Json -Compress" % pid],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=True,
    )
    line = json.loads(result.stdout)
    if not isinstance(line, str):
        raise RuntimeError("Native client command line is unavailable")
    api = ctypes.WinDLL("shell32", use_last_error=True)
    api.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    api.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    values = api.CommandLineToArgvW(line, ctypes.byref(count))
    if not values:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [values[index] for index in range(count.value)]
    finally:
        kernel = ctypes.WinDLL("kernel32")
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(values)


def process_context(pid):
    if os.name != "nt":
        entries = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
        values = dict(entry.split(b"=", 1) for entry in entries if b"=" in entry)
        return {'profile': str(Path(os.fsdecode(values.get(HOST.home_variable.encode(),
                                                            os.fsencode(Path.home() / HOST.home_directory)))).resolve()),
                RECORD_ENV: os.fsdecode(values.get(RECORD_ENV.encode(), b'')),
                RECORD_PATH_ENV: os.fsdecode(values.get(RECORD_PATH_ENV.encode(), b''))}
    import ctypes
    from ctypes import wintypes

    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("Reading an unmanaged Codex client requires 64-bit Python on Windows")

    class Basic(ctypes.Structure):
        _fields_ = [("reserved", ctypes.c_void_p), ("peb", ctypes.c_void_p),
                    ("reserved2", ctypes.c_void_p * 2), ("pid", ctypes.c_size_t), ("reserved3", ctypes.c_void_p)]

    class Region(ctypes.Structure):
        _fields_ = [("base", ctypes.c_void_p), ("allocation", ctypes.c_void_p), ("protection", wintypes.DWORD),
                    ("size", ctypes.c_size_t), ("state", wintypes.DWORD), ("protect", wintypes.DWORD),
                    ("kind", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    native = ctypes.WinDLL("ntdll")
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(Region), ctypes.c_size_t]
    kernel.VirtualQueryEx.restype = ctypes.c_size_t
    native.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                                 wintypes.ULONG, ctypes.c_void_p]
    handle = kernel.OpenProcess(0x410, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        basic = Basic()
        if native.NtQueryInformationProcess(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic), None):
            raise RuntimeError("Native client process information is unavailable")

        def read(address, size):
            buffer = ctypes.create_string_buffer(size)
            done = ctypes.c_size_t()
            if not kernel.ReadProcessMemory(handle, address, buffer, size, ctypes.byref(done)):
                raise ctypes.WinError(ctypes.get_last_error())
            return buffer.raw[:done.value]

        parameters = int.from_bytes(read(basic.peb + 0x20, 8), "little")
        address = int.from_bytes(read(parameters + 0x80, 8), "little")
        region = Region()
        if not kernel.VirtualQueryEx(handle, address, ctypes.byref(region), ctypes.sizeof(region)):
            raise ctypes.WinError(ctypes.get_last_error())
        raw = read(address, min(65536, region.base + region.size - address))
        end = next((index + 4 for index in range(0, len(raw) - 3, 2) if raw[index:index + 4] == b"\0" * 4), None)
        if end is None:
            raise RuntimeError("Native client environment is incomplete")
        text = raw[:end].decode("utf-16-le")
        values = {}
        for entry in text.split('\0'):
            if not entry:
                break
            key, separator, value = entry.partition('=')
            if separator:
                values[key.casefold()] = value
        return {'profile': str(Path(values.get(HOST.home_variable.casefold()) or Path.home() / HOST.home_directory).resolve()),
                RECORD_ENV: values.get(RECORD_ENV.casefold(), ''),
                RECORD_PATH_ENV: values.get(RECORD_PATH_ENV.casefold(), '')}
    finally:
        kernel.CloseHandle(handle)


def process_profile(pid):
    return Path(process_context(pid)['profile'])
