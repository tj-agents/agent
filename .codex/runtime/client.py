"""Bind managed Codex launches to their frontend process (Python 3.9+)."""

import errno
from collections.abc import Mapping
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

from codex import frontend_input, hook_trust
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


class RecipientPending(RuntimeError):
    pass


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


def recorder_policy_required(arguments, environment):
    if environment.get("CODEX_EXEC_SERVER_URL"):
        return True
    if os.name == "nt":
        import ctypes
        if ctypes.windll.shell32.IsUserAnAdmin():
            return True
    options = {"-c", "--config", "-p", "--profile", "--enable", "--disable", "--search",
               "--oss", "--strict-config", "--dangerously-bypass-hook-trust"}
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            break
        option, separator, _setting = value.partition("=")
        if option in options or value.startswith(("-c", "-p")):
            return True
        index += 2 if option in VALUE_OPTIONS and not separator else 1
    return False


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


def prepare(arguments, project, environment, executable=None, handoff=False, hook_roots=None):
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
    if not handoff and '--no-daemon' not in arguments and not any(
            value.split('=')[0] == '--remote' for value in arguments):
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
        'schema_version': 2 if handoff else 1, 'token': token, 'host': HOST.value, 'backend': backend,
        'profile': str(profile_directory(values)), 'launch_cwd': str(Path(project).resolve()),
        'created_at': time.time(), 'recording_path': values[RECORD_PATH_ENV],
        'title': values.get('AGENT_CLI_TAB_TITLE'),
        'terminal_environment': {key: values[key] for key in (
            'TMUX', 'TMUX_PANE', 'KITTY_WINDOW_ID', 'KITTY_LISTEN_ON', 'KONSOLE_DBUS_SERVICE',
            'KONSOLE_DBUS_SESSION', 'KONSOLE_DBUS_WINDOW', 'WT_SESSION', 'AGENT_WINDOWS_TERMINAL_TAB_ID',
        ) if key in values},
    }
    if handoff:
        from codex.backend import Lease, identity
        import required_plugins

        if '--no-daemon' in arguments or any(value.split('=')[0] == '--remote' for value in arguments):
            raise RuntimeError('A handoff owns its native endpoint; an external backend cannot be selected')
        effective, configuration = required_plugins.launch_context(Path(project), HOST, arguments)
        if effective is None or effective.resolve() != Path(project).resolve():
            raise RuntimeError('Handoff launch checkout does not match its verified native configuration')
        backend_environment = {key: value for key, value in values.items()
                               if key not in (RECORD_ENV, RECORD_PATH_ENV)}
        lease = Lease(executable, Path(project), backend_environment, path.parent, list(configuration))
        try:
            ticket.update(backend=lease.backend, supervisor=lease.supervisor, wrapper=identity(os.getpid()),
                          executable=str(Path(executable).resolve()), _lease=lease)
            await_backend(ticket, hook_roots or {})
        except BaseException:
            lease.close()
            raise
        arguments = ['--remote', 'unix://' + lease.backend['socket_path'], *arguments]
    prepared = arguments
    if backend is None or recorder_policy_required(arguments, values):
        prepared = ['--config', 'shell_environment_policy.set.' + RECORD_ENV + '=""',
                    '--config', 'shell_environment_policy.set.' + RECORD_PATH_ENV + '=""', *arguments]
    return prepared, values, ticket


def attach(ticket, pid):
    if ticket is None:
        return
    path = binding_path(ticket['token'])
    value = {key: item for key, item in ticket.items() if not key.startswith('_')}
    value.update(pid=pid, recorded_at=time.time())
    if (ticket.get('backend') or {}).get('kind') == 'handoff':
        from codex.backend import identity

        value['frontend'] = identity(pid)
        ticket['frontend'] = value['frontend']
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    ticket['_attached'] = True
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write('\n')


def release(ticket):
    if ticket is None:
        return
    path = binding_path(ticket['token'])
    if ticket.get('_lease') is not None:
        ticket['_lease'].close()
        return
    if ticket.get('_attached'):
        path.unlink(missing_ok=True)
        path.with_name('events.jsonl').unlink(missing_ok=True)
        path.with_name('shell-continuation.json').unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def server_for(record, timeout=15):
    return hook_trust.AppServer(record['executable'], Path(record['launch_cwd']),
                               environment={HOST.home_variable: record['profile']}, existing=True,
                               socket_path=record['backend']['socket_path'], timeout=timeout)


def verify_endpoint(backend: Mapping[str, object]) -> None:
    endpoint = backend.get("socket_path")
    if not isinstance(endpoint, str) or not Path(endpoint).is_absolute():
        raise RuntimeError("Handoff native endpoint is not an absolute path")
    socket = Path(endpoint)
    for path, key in ((socket.parent, "parent_identity"), (socket, "socket_identity")):
        value = path.lstat()
        if [value.st_dev, value.st_ino] != backend.get(key):
            raise RuntimeError("Handoff native endpoint was replaced")


def await_backend(ticket, roots):
    from tj_cli import processes

    processes.require_live(
        ticket["backend"], "Owned native backend exited before readiness"
    )
    verify_endpoint(ticket["backend"])
    with server_for(ticket, 10) as server:
        required = {
            identity: hook_trust.required_definitions(root, identity)
            for identity, root in roots.items()
        }
        hook_trust.require_ready(
            server.request("hooks/list", {"cwds": [ticket["launch_cwd"]]}),
            Path(ticket["launch_cwd"]),
            required,
        )
        if server.request("thread/loaded/list", {}).get("data"):
            raise RuntimeError(
                "A new handoff backend already contains a loaded conversation"
            )
    processes.require_live(
        ticket["backend"], "Owned native backend exited during readiness"
    )
    verify_endpoint(ticket["backend"])


def native_contains(server, session_id, identity):
    cursor, seen = None, set()
    for _ in range(128):
        params = {'threadId': session_id, 'limit': 100, 'sortDirection': 'desc'}
        if cursor is not None:
            params['cursor'] = cursor
        page = server.request('thread/items/list', params)
        for row in page['data']:
            item = row['item']
            if item.get('type') == 'userMessage' and item.get('clientId') == identity:
                return True
        cursor = page.get('nextCursor')
        if cursor is None:
            return False
        if not isinstance(cursor, str) or cursor in seen:
            raise RuntimeError('Native frontend attribution pagination did not advance')
        seen.add(cursor)
    raise RuntimeError('Native frontend attribution exceeds the history scan limit')


def verify_owned(record, *, table=None, peer_state=None):
    from tj_cli import processes

    if table is None:
        from startup_binding import register_session

        table = register_session.process_table()
        lookup = register_session.build_process_lookup()
        def peer_state(pid):
            info = lookup(pid)
            return ('present', info) if info is not None else ('absent', None)

    if (record.get('schema_version') != 2 or record.get('host') != HOST.value
            or (record.get('backend') or {}).get('kind') != 'handoff'):
        raise RuntimeError('Unsupported owned handoff binding')
    path = binding_path(record['token'])
    event_path = path.with_name('events.jsonl')
    if comparable_path(Path(record['recording_path'])) != comparable_path(event_path):
        raise RuntimeError('Handoff recording does not belong to its launch token')
    frontend = processes.require_live(record['frontend'], 'Handoff frontend identity changed')
    supervisor = processes.require_live(record['supervisor'], 'Handoff supervisor identity changed')
    backend = processes.require_live(record['backend'], 'Handoff backend identity changed')
    wrapper = processes.require_live(record['wrapper'], 'Handoff wrapper identity changed')
    if (frontend.pid != record['pid'] or frontend.ppid != wrapper.pid or backend.ppid != supervisor.pid
            or not processes.descends_from(record['supervisor'], record['wrapper'])):
        raise RuntimeError('Handoff process ownership changed')
    endpoint = record['backend']['socket_path']
    verify_endpoint(record['backend'])
    for info, native in ((frontend, True), (backend, False)):
        arguments = process_arguments(info.pid)
        context = process_context(info.pid)
        if (not arguments or comparable_path(Path(arguments[0]).resolve())
                != comparable_path(Path(record['executable']).resolve())
                or comparable_path(Path(context['profile'])) != comparable_path(Path(record['profile']))):
            raise RuntimeError('Handoff native executable or profile changed')
        option = '--remote' if native else '--listen'
        positions = [index for index, value in enumerate(arguments) if value == option]
        if (len(positions) != 1 or arguments[positions[0] + 1:positions[0] + 2] != ['unix://' + endpoint]
                or (native and (not is_session(arguments[1:]) or '--no-daemon' in arguments))
                or (not native and command_name(arguments[1:]) != 'app-server')):
            raise RuntimeError('Handoff native process selected a different endpoint')
        if native:
            if (context.get(RECORD_ENV) not in ('1', 'true', 'TRUE', 'yes', 'YES')
                    or comparable_path(Path(context.get(RECORD_PATH_ENV, ''))) != comparable_path(event_path)):
                raise RuntimeError('Handoff frontend recording changed')
        elif context.get(RECORD_ENV) or context.get(RECORD_PATH_ENV):
            raise RuntimeError('Handoff backend inherited frontend recording state')
    if table is not None:
        for other in table.values():
            if other.pid == frontend.pid or Path(other.name).stem.casefold() != HOST.value:
                continue
            other_path = peer_recording_path(other, peer_state)
            if other_path is not None and comparable_path(Path(other_path)) == comparable_path(event_path):
                raise RuntimeError('Multiple native processes share a handoff frontend recording')
    try:
        anchor = latest_input(event_path)
    except FileNotFoundError as error:
        raise RecipientPending('Handoff frontend has not recorded its initial input') from error
    if not anchor:
        raise RecipientPending('Handoff frontend has no unswitched native input')
    return anchor


def bind_recipient(ticket, table, peer_state):
    path = binding_path(ticket['token'])
    record = json.loads(path.read_text(encoding='utf-8'))
    anchor = verify_owned(record, table=table, peer_state=peer_state)
    matches = []
    with server_for(record) as server:
        loaded = server.request('thread/loaded/list', {}).get('data', [])
        if not isinstance(loaded, list) or len(loaded) > 128:
            raise RuntimeError('Owned handoff loaded-thread inventory is invalid')
        for session_id in loaded:
            thread = server.request('thread/read', {'threadId': session_id, 'includeTurns': False})['thread']
            if (thread.get('id') != session_id or thread.get('ephemeral') is True
                    or isinstance(thread.get('source'), dict)
                    or thread.get('threadSource') in ('agent', 'subagent')
                    or comparable_path(Path(thread.get('cwd', '')).resolve())
                    != comparable_path(Path(record['launch_cwd']).resolve())):
                continue
            if native_contains(server, session_id, anchor):
                matches.append({'thread_id': session_id, 'cwd': thread['cwd'], 'frontend_message_id': anchor})
        if not matches:
            raise RecipientPending('Handoff input has not reached a loaded native conversation')
        if len(matches) != 1:
            raise RuntimeError('Handoff input does not identify exactly one loaded native conversation')
        submitted = current_input(server, matches[0]['thread_id'])
    if verify_owned(record, table=table, peer_state=peer_state) != anchor:
        raise RuntimeError('Handoff frontend changed while binding its recipient')
    record.update(recipient=matches[0], verified_at=time.time())
    from codex.backend import write_record

    write_record(path, record)
    ticket['recipient'] = record['recipient']
    return record, {**submitted, 'frontend_message_id': anchor}


def launch_binding(receipt_path, thread_id, cwd, profile):
    receipt = json.loads(Path(receipt_path).read_text(encoding='utf-8'))
    reference = receipt.get('client_binding')
    if receipt.get('state') != 'running' or not isinstance(reference, dict):
        raise RuntimeError('Launch receipt has no verified running handoff recipient')
    record = json.loads(binding_path(reference.get('token')).read_text(encoding='utf-8'))
    recipient = record.get('recipient') or {}
    if (reference.get('recipient') != recipient or recipient.get('thread_id') != thread_id
            or comparable_path(Path(recipient.get('cwd', '')).resolve()) != comparable_path(Path(cwd).resolve())
            or comparable_path(Path(record['profile']).resolve()) != comparable_path(Path(profile).resolve())):
        raise RuntimeError('Launch receipt recipient, checkout or profile does not match')
    return freeze_binding(record)


def recipient_binding(thread_id, cwd, profile):
    expected_cwd = comparable_path(Path(cwd).resolve())
    expected_profile = comparable_path(Path(profile).resolve())
    state = Path(os.environ.get('AGENT_STATE_DIRECTORY') or Path.home() / '.agents-state')
    matches = []
    for path in (state / 'cli-clients').glob('*/client.json'):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        recipient = record.get('recipient')
        if (record.get('schema_version') != 2 or record.get('host') != HOST.value
                or (record.get('backend') or {}).get('kind') != 'handoff'
                or not isinstance(recipient, dict)
                or recipient.get('thread_id') != thread_id
                or comparable_path(Path(recipient.get('cwd', '')).resolve()) != expected_cwd
                or comparable_path(Path(record.get('profile', '')).resolve()) != expected_profile):
            continue
        matches.append((path, record))
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one verified managed Codex binding for this recipient')
    path, record = matches[0]
    if path != binding_path(record.get('token')):
        raise RuntimeError('Managed Codex binding is outside its launch token')
    return freeze_binding(record)


def freeze_binding(record):
    frozen = {key: record[key] for key in ('token', 'host', 'profile', 'launch_cwd', 'recording_path',
                                         'frontend', 'wrapper', 'supervisor', 'backend', 'recipient', 'executable')}
    frozen['frontend_message_id'] = verify_owned(record)
    return frozen


def frozen_binding(frozen):
    record = json.loads(binding_path(frozen.get('token')).read_text(encoding='utf-8'))
    if freeze_binding(record) != frozen:
        raise RuntimeError('Frozen handoff endpoint or process identity changed')
    return record


def latest_input(path):
    recording = frontend_input.FrontendInput(Path(path), VIEW_CHANGES)
    while True:
        offset = recording.stream.offset
        recording.poll()
        if recording.stream.settled():
            break
        if recording.stream.offset == offset:
            raise RuntimeError('Native frontend recording is incomplete or oversized')
    if recording.headers != 1:
        raise RuntimeError('Native frontend recording has no unique startup header')
    return frontend_input.attributed_input(recording)


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
        if error.errno != errno.ESRCH and getattr(error, "winerror", None) not in (6, 87, 299):
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
    state = Path(os.environ.get('AGENT_STATE_DIRECTORY') or Path.home() / '.agents-state')
    owned = []
    ordinary_frontend = False
    for path in (state / 'cli-clients').glob('*/client.json'):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        backend = record.get('backend') if isinstance(record, dict) else None
        if (isinstance(record, dict) and record.get('schema_version') == 1
                and record.get('pid') == nearest.pid and path == binding_path(record.get('token'))):
            ordinary_frontend = True
        if (isinstance(record, dict) and isinstance(backend, dict)
                and (backend.get('kind') == 'handoff' or record.get('schema_version') == 2)
                and nearest.pid in (backend.get('pid'), record.get('pid'))):
            if path != binding_path(record.get('token')):
                raise RuntimeError('Owned handoff binding is outside its launch token')
            owned.append(record)
    if owned:
        if len(owned) != 1:
            raise RuntimeError('Native process belongs to multiple handoff bindings')
        record = owned[0]
        if ((record.get('recipient') or {}).get('thread_id') != session_id
                or comparable_path(Path(record['profile'])) != comparable_path(profile_directory())
                or (project is not None and comparable_path(Path(project).resolve())
                    != comparable_path(Path(record['launch_cwd']).resolve()))):
            raise RuntimeError('Handoff recipient, checkout or profile does not match this native client')
        anchor = verify_owned(record, table=table, peer_state=peer_state)
        with server_for(record) as server:
            if session_id not in server.request('thread/loaded/list', {}).get('data', []):
                raise RuntimeError('Handoff recipient is no longer loaded in its owned backend')
            if not native_contains(server, session_id, anchor):
                raise RuntimeError('Frontend input no longer belongs to the bound handoff conversation')
            submitted = current_input(server, session_id)
        if comparable_path(Path(submitted['native_cwd']).resolve()) != comparable_path(Path(record['launch_cwd']).resolve()):
            raise RuntimeError('Handoff native checkout changed')
        return lookup(record['pid']), {**record, **submitted, 'frontend_message_id': anchor}
    arguments = process_arguments(nearest.pid)
    if (not ordinary_frontend and any(value.split('=')[0] == '--remote' for value in arguments)):
        raise RuntimeError('Remote frontend has no verified owned launch binding')
    private = bool(arguments and is_session(arguments[1:]) and '--no-daemon' in arguments)
    # `nearest` is the actual frontend ancestor. A shared daemon is a
    # separate process, so verify its identity independently and bind the
    # selected recorded frontend to both identities below.
    backend = None if private else daemon_identity(**(
        {'expected_pid': nearest.pid} if command_name(arguments[1:]) == 'app-server' else {}))
    executable = arguments[0]
    with hook_trust.AppServer(executable, Path(project) if project is not None else Path.cwd(),
                             existing=not private, timeout=15) as server:
        submitted = current_input(server, session_id)
    matches = []
    for path in (state / 'cli-clients').glob('*/client.json'):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(record, dict) or path != binding_path(record.get('token'))
                    or comparable_path(Path(record.get('profile', ''))) != comparable_path(profile_directory())
                    or record.get('backend') != backend
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
            event_path = path.with_name('events.jsonl')
            event_identity = comparable_path(event_path)
            if (comparable_path(Path(record.get('recording_path', ''))) != event_identity
                    or comparable_path(Path(context.get(RECORD_PATH_ENV, ''))) != event_identity
                    or context.get(RECORD_ENV) not in ('1', 'true', 'TRUE', 'yes', 'YES')
                    or Path(context['profile']) != profile_directory()):
                continue
            arguments = process_arguments(info.pid)
            anchor = latest_input(event_path)
            if (not arguments or not is_session(arguments[1:])
                    or ('--no-daemon' in arguments) != private
                    or not anchor):
                continue
            if anchor != submitted['client_message_id']:
                with hook_trust.AppServer(executable, Path(project) if project is not None else Path.cwd(),
                                         existing=not private, timeout=15) as server:
                    if not native_contains(server, session_id, anchor):
                        continue
            for other in table.values():
                if other.pid == info.pid or Path(other.name).stem.casefold() != HOST.value:
                    continue
                peer_path = peer_recording_path(other, peer_state)
                if peer_path is not None and comparable_path(Path(peer_path)) == event_identity:
                    raise RuntimeError('Multiple native processes share a frontend recording; preserving every client')
            if (info.pid != nearest.pid
                    and (private or nearest.pid != backend['pid'])):
                continue
            matches.append((info, {**record, 'frontend_message_id': anchor}))
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
        if latest_input(self.entry['recording_path']) != self.entry.get('frontend_message_id', self.entry['client_message_id']):
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

    class UnicodeString(ctypes.Structure):
        _fields_ = [("length", wintypes.USHORT), ("maximum_length", wintypes.USHORT),
                    ("buffer", ctypes.c_void_p)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    native = ctypes.WinDLL("ntdll")
    native.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                                 wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    native.NtQueryInformationProcess.restype = ctypes.c_long
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = wintypes.ULONG()
        native.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(required))
        if not ctypes.sizeof(UnicodeString) <= required.value <= 131072:
            raise RuntimeError("Native client command line is unavailable")
        buffer = ctypes.create_string_buffer(required.value)
        if native.NtQueryInformationProcess(handle, 60, buffer, len(buffer), ctypes.byref(required)):
            raise RuntimeError("Native client command line is unavailable")
        command = UnicodeString.from_buffer(buffer)
        start = command.buffer or 0
        if (command.length % 2 or command.length > command.maximum_length
                or start < ctypes.addressof(buffer) + ctypes.sizeof(UnicodeString)
                or start + command.length > ctypes.addressof(buffer) + len(buffer)):
            raise RuntimeError("Native client command line is invalid")
        line = ctypes.wstring_at(start, command.length // 2)
    finally:
        kernel.CloseHandle(handle)
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
    return _windows_process_context(pid)


def _windows_process_context(pid, environment=True):
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
        command = read(parameters + 0x70, 16)
        length, address = int.from_bytes(command[:2], 'little'), int.from_bytes(command[8:], 'little')
        if not address or not length or length % 2:
            raise RuntimeError('Native client command line is unavailable')
        command_line = read(address, length).decode('utf-16-le')
        if not environment:
            return {'command_line': command_line}
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
