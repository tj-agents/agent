"""Run one bounded command and submit its result to the owning Codex thread (Python 3.11+)."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Protocol, TypeVar
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex import client, hook_trust
import package_root


T = TypeVar('T')
if sys.platform == 'win32':
    HIDDEN_PROCESS_FLAGS = subprocess.CREATE_NO_WINDOW
else:
    HIDDEN_PROCESS_FLAGS = 0


class Server(Protocol):
    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Target:
    session_id: str
    profile: str
    cwd: str
    backend: dict[str, Any]
    executable: str


@dataclass(frozen=True)
class Request:
    target: Target
    identity: str
    purpose: str
    cwd: str
    timeout: float
    command: tuple[str, ...]

    def value(self) -> dict[str, Any]:
        result = asdict(self)
        result['command'] = list(self.command)
        return result


def state_directory() -> Path:
    return Path(os.environ.get('AGENT_STATE_DIRECTORY') or Path.home() / '.agents-state').resolve()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(retry_sharing(lambda: path.read_text(encoding='utf-8')))
    if not isinstance(value, dict):
        raise RuntimeError(f'Invalid listener record: {path}')
    return value


def retry_sharing(operation: Callable[[], T]) -> T:
    deadline = time.monotonic() + 3
    while True:
        try:
            return operation()
        except OSError as error:
            if getattr(error, 'winerror', None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def write_json(path: Path, value: dict[str, Any]) -> None:
    descriptor, staging = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        retry_sharing(lambda: os.replace(staging, path))
    finally:
        retry_sharing(lambda: Path(staging).unlink(missing_ok=True))


def path_key(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def registration(state: Path, session_id: str, profile: str) -> dict[str, Any]:
    if str(uuid.UUID(session_id)) != session_id:
        raise RuntimeError('An exact canonical Codex thread UUID is required')
    value = read_json(state / 'cli-sessions' / (session_id + '.json'))
    if (value.get('host') != 'codex' or value.get('session_id') != session_id
            or not isinstance(value.get('profile'), str)
            or path_key(value['profile']) != path_key(profile)
            or not isinstance(value.get('cwd'), str) or not value['cwd']):
        raise RuntimeError('The registered Codex session, host or profile does not match')
    return value


def verify_backend(target: Target) -> None:
    if os.name == 'nt':
        executable = native_executable(target.profile, target.backend)
        if path_key(executable) != path_key(target.executable):
            raise RuntimeError('The pinned native Codex daemon or executable changed')
        return
    backend = client.daemon_identity({'CODEX_HOME': target.profile})
    executable = native_executable(target.profile, backend)
    if backend != target.backend or path_key(executable) != path_key(target.executable):
        raise RuntimeError('The pinned native Codex daemon or executable changed')
    verify_mapped_image(backend['pid'], target.executable)


def native_executable(profile: str, backend: dict[str, Any]) -> str:
    if os.name == 'nt':
        from codex import listener_windows

        return listener_windows.daemon_executable(profile, backend)
    arguments = client.process_arguments(backend['pid'])
    if not arguments:
        raise RuntimeError('The native Codex daemon executable is unavailable')
    return str(Path(arguments[0]).resolve(strict=True))


def verify_mapped_image(pid: int, executable: str) -> None:
    if os.name != 'nt':
        running = (Path('/proc') / str(pid) / 'exe').stat()
        installed = Path(executable).stat()
        if (running.st_dev, running.st_ino) != (installed.st_dev, installed.st_ino):
            raise RuntimeError('The native daemon still maps an executable that has been replaced on disk')


def verify_running_version(server: hook_trust.AppServer, target: Target) -> None:
    initialized = server.initialization
    user_agent = initialized.get('userAgent')
    version = re.match(r'[^\s/]+/([^\s]+)\s', user_agent) if isinstance(user_agent, str) else None
    home = initialized.get('codexHome')
    if (version is None or not isinstance(home, str)
            or path_key(home) != path_key(target.profile)):
        raise RuntimeError('The running Codex daemon version or profile could not be verified')
    output = subprocess.run([target.executable, '--version'], cwd=target.cwd,
                            env=dict(os.environ, CODEX_HOME=target.profile),
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding='utf-8',
                            timeout=10, check=True,
                            creationflags=HIDDEN_PROCESS_FLAGS).stdout.strip()
    if output != 'codex-cli ' + version.group(1):
        raise RuntimeError('The running Codex daemon and on-disk native executable versions differ')


def verify_target(server: Server, target: Target, state: Path) -> None:
    value = registration(state, target.session_id, target.profile)
    if path_key(value['cwd']) != path_key(target.cwd) or value.get('backend') != target.backend:
        raise RuntimeError('The registered Codex target or backend changed')
    verify_backend(target)
    response = server.request('thread/read', {'threadId': target.session_id, 'includeTurns': False})
    thread = response.get('thread')
    if (not isinstance(thread, dict) or thread.get('id') != target.session_id
            or not isinstance(thread.get('cwd'), str)
            or path_key(thread['cwd']) != path_key(target.cwd)
            or thread.get('canAcceptDirectInput') is not True
            or not isinstance(thread.get('status'), dict)
            or thread['status'].get('type') not in ('idle', 'active')
            or isinstance(thread.get('source'), dict)
            or thread.get('threadSource') in ('agent', 'subagent')):
        raise RuntimeError('The exact loaded CLI thread cannot accept native completion input')
    cursor = None
    seen: set[str] = set()
    for _ in range(128):
        params: dict[str, Any] = {'limit': 100}
        if cursor is not None:
            params['cursor'] = cursor
        page = server.request('thread/loaded/list', params)
        data = page.get('data')
        if not isinstance(data, list):
            raise RuntimeError('Native loaded-thread pagination is unsupported')
        if target.session_id in data:
            return
        cursor = page.get('nextCursor')
        if cursor is None:
            raise RuntimeError('The pinned Codex thread is no longer loaded')
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise RuntimeError('Native loaded-thread pagination did not advance')
        seen.add(cursor)
    raise RuntimeError('Native loaded-thread pagination exceeded its bound')


def connection(target: Target) -> hook_trust.AppServer:
    return hook_trust.AppServer(target.executable, Path(target.cwd),
                                environment={'CODEX_HOME': target.profile}, timeout=15, existing=True)


def verify_capability(target: Target) -> None:
    with tempfile.TemporaryDirectory(prefix='agent-listener-schema-') as directory:
        subprocess.run(
            [target.executable, 'app-server', 'generate-json-schema', '--experimental', '--out', directory],
            cwd=target.cwd, env=dict(os.environ, CODEX_HOME=target.profile),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, check=True,
            creationflags=HIDDEN_PROCESS_FLAGS,
        )
        schema = read_json(Path(directory) / 'v2' / 'TurnStartParams.json')
        if not isinstance(schema.get('properties'), dict) or 'toolOutput' not in schema['properties']:
            raise RuntimeError('The pinned native Codex daemon does not support toolOutput completion')


def capture_target(state: Path) -> Target:
    session_id = os.environ.get('CODEX_THREAD_ID')
    if not session_id:
        raise RuntimeError('CODEX_THREAD_ID is required; no listener command was started')
    profile = str(client.profile_directory())
    value = registration(state, session_id, profile)
    if os.name == 'nt':
        backend = value.get('backend')
        if (not isinstance(backend, dict) or type(backend.get('pid')) is not int or backend['pid'] <= 0
                or not isinstance(backend.get('process_start_time'), str) or not backend['process_start_time']):
            raise RuntimeError('The session has no pinned native Codex daemon identity')
    else:
        backend = client.daemon_identity({'CODEX_HOME': profile})
    if value.get('backend') != backend:
        raise RuntimeError('The session does not identify this native Codex daemon')
    executable = Path(native_executable(profile, backend)).resolve(strict=True)
    if not executable.is_file():
        raise RuntimeError('The native daemon executable is unavailable')
    target = Target(session_id, profile, str(Path(value['cwd']).resolve()), dict(backend), str(executable))
    with connection(target) as server:
        verify_target(server, target, state)
        verify_running_version(server, target)
    verify_capability(target)
    with connection(target) as server:
        verify_target(server, target, state)
        verify_running_version(server, target)
    return target


def load_request(job: Path) -> Request:
    value = read_json(job / 'request.json')
    target = value.get('target')
    if not isinstance(target, dict) or set(target) != {'session_id', 'profile', 'cwd', 'backend', 'executable'}:
        raise RuntimeError('The persisted listener target is invalid')
    if (any(not isinstance(target[key], str) or not target[key]
            for key in ('session_id', 'profile', 'cwd', 'executable'))
            or not isinstance(target['backend'], dict)
            or type(target['backend'].get('pid')) is not int
            or target['backend']['pid'] <= 0
            or not isinstance(target['backend'].get('process_start_time'), str)
            or not target['backend']['process_start_time']):
        raise RuntimeError('The persisted listener backend is invalid')
    command = value.get('command')
    timeout = value.get('timeout')
    if (any(not isinstance(value.get(key), str) or not value[key].strip()
            for key in ('identity', 'purpose', 'cwd'))
            or not isinstance(command, list) or not command
            or any(not isinstance(argument, str) or '\0' in argument for argument in command)
            or not command[0] or not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or not 0 < timeout <= 86400):
        raise RuntimeError('The persisted listener command is invalid')
    request = Request(Target(**target), value['identity'], value['purpose'], value['cwd'],
                      float(timeout), tuple(command))
    if job.name != job_key(request.target.session_id, request.identity):
        raise RuntimeError('The listener job directory does not match its identity')
    return request


def job_key(session_id: str, identity: str) -> str:
    return hashlib.sha256((session_id + '\0' + identity).encode('utf-8')).hexdigest()


def status(job: Path) -> dict[str, Any]:
    value = read_json(job / 'state.json')
    return {**value, 'job': str(job), 'state': str(job / 'state.json'),
            'output': str(job / 'output.log')}


def detach(job: Path) -> None:
    root = package_root.find_package_root(Path(__file__).resolve().parent)
    path = root / '.agents/agent/utility/peer/scripts/session_close.py'
    spec = importlib.util.spec_from_file_location('agent_listener_session_close', path)
    if spec is None or spec.loader is None:
        raise RuntimeError('The packaged Agent watcher launcher is unavailable')
    sys.path.insert(0, str(path.parent))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    arguments = ['worker', '--job', str(job)]
    if os.name == 'nt':
        from codex import listener_windows

        with listener_windows.EnvironmentPipe() as pipe:
            module.start_agent_watcher(Path(__file__).resolve(), [*arguments, '--environment-pipe', pipe.name])
            pipe.send(dict(os.environ))
    else:
        module.start_agent_watcher(Path(__file__).resolve(), arguments)


def start(identity: str, purpose: str, cwd: Path, timeout: float,
          command: tuple[str, ...]) -> dict[str, Any]:
    if (not identity.strip() or not purpose.strip() or not command or not command[0]
            or any('\0' in argument for argument in command)
            or not math.isfinite(timeout) or not 0 < timeout <= 86400):
        raise RuntimeError('A job identity, purpose, command and timeout in (0, 86400] are required')
    cwd = cwd.resolve(strict=True)
    if not cwd.is_dir():
        raise RuntimeError('The listener command directory is unavailable')
    state = state_directory()
    target = capture_target(state)
    request = Request(target, identity, purpose, str(cwd), timeout, command)
    parent = state / 'listeners'
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    job = parent / job_key(target.session_id, identity)
    try:
        job.mkdir(mode=0o700)
    except FileExistsError:
        if load_request(job) != request:
            raise RuntimeError('This listener identity already owns a different request')
        return status(job)
    write_json(job / 'request.json', request.value())
    write_json(job / 'state.json', {'status': 'starting', 'identity': identity, 'acknowledged': False})
    try:
        detach(job)
    except Exception as error:
        write_json(job / 'state.json', {'status': 'unavailable', 'identity': identity,
                                      'acknowledged': False, 'error': str(error)})
        return status(job)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = status(job)
        if value['status'] != 'starting':
            return value
        time.sleep(0.05)
    return status(job)


def output_tail(path: Path) -> str:
    with path.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - 8192))
        return stream.read(8192).decode('utf-8', errors='replace')


def run_command(request: Request, output: Path, environment: dict[str, str] | None = None) -> dict[str, Any]:
    environment = dict(os.environ) if environment is None else environment
    with output.open('wb') as stream:
        try:
            if sys.platform == 'win32':
                from codex import listener_windows

                returncode, timed_out = listener_windows.run(request.command, request.cwd,
                                                             request.timeout, stream, environment)
                return {'outcome': 'timeout' if timed_out else ('success' if returncode == 0 else 'failed'),
                        'returncode': returncode}
            else:
                process = subprocess.Popen(request.command, cwd=request.cwd, stdin=subprocess.DEVNULL,
                                           stdout=stream, stderr=subprocess.STDOUT, shell=False,
                                           env=environment, start_new_session=True)
                try:
                    returncode = process.wait(timeout=request.timeout)
                    return {'outcome': 'success' if returncode == 0 else 'failed', 'returncode': returncode}
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                    return {'outcome': 'timeout', 'returncode': process.returncode}
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            stream.write(str(error).encode('utf-8', errors='replace'))
            return {'outcome': 'failed', 'returncode': None}


def worker(job: Path, environment: dict[str, str] | None = None) -> dict[str, Any]:
    request = load_request(job)
    try:
        with (job / 'execution.json').open('x', encoding='utf-8') as stream:
            json.dump({'pid': os.getpid(), 'started_at': time.time()}, stream)
    except FileExistsError:
        return status(job)
    state = job.parent.parent
    value: dict[str, Any] = {'identity': request.identity, 'acknowledged': False}
    try:
        with connection(request.target) as server:
            verify_target(server, request.target, state)
            verify_running_version(server, request.target)
        value['status'] = 'running'
        write_json(job / 'state.json', value)
        value.update(run_command(request, job / 'output.log', environment))
        with connection(request.target) as server:
            verify_target(server, request.target, state)
            verify_running_version(server, request.target)
            payload = {'identity': request.identity, 'purpose': request.purpose,
                       'outcome': value['outcome'], 'returncode': value['returncode'],
                       'output': str(job / 'output.log'), 'tail': output_tail(job / 'output.log')}
            value['status'] = 'delivering'
            write_json(job / 'state.json', value)
            try:
                result = server.request('turn/start', {
                    'threadId': request.target.session_id, 'input': [],
                    'toolOutput': {'name': 'agent_listener', 'namespace': None,
                                   'output': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))},
                })
                turn = result.get('turn')
                if not isinstance(turn, dict) or not isinstance(turn.get('id'), str) or not turn['id']:
                    raise RuntimeError('The native submission response did not confirm acceptance')
                value.update(status='accepted', turn_id=turn['id'])
            except Exception as error:
                value.update(status='delivery-unknown', error=str(error))
    except Exception as error:
        if value.get('status') == 'delivering':
            value.update(status='delivery-unknown', error=str(error))
        elif value.get('status') not in ('accepted', 'delivery-unknown'):
            value.update(status='unavailable', error=str(error))
    write_json(job / 'state.json', value)
    return status(job)


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        raise RuntimeError('The Codex listener requires Python 3.11 or newer')
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    launch = commands.add_parser('start')
    launch.add_argument('--identity', required=True)
    launch.add_argument('--purpose', required=True)
    launch.add_argument('--cwd', required=True, type=Path)
    launch.add_argument('--timeout', required=True, type=float)
    launch.add_argument('command', nargs=argparse.REMAINDER)
    for operation in ('status', 'worker'):
        command = commands.add_parser(operation, help=argparse.SUPPRESS if operation == 'worker' else None)
        command.add_argument('--job', required=True, type=Path)
        if operation == 'worker':
            command.add_argument('--environment-pipe')
    arguments = parser.parse_args(argv)
    try:
        if arguments.operation == 'start':
            command = arguments.command[1:] if arguments.command[:1] == ['--'] else arguments.command
            result = start(arguments.identity, arguments.purpose, arguments.cwd,
                           arguments.timeout, tuple(command))
        elif arguments.operation == 'worker':
            environment = None
            if os.name == 'nt':
                from codex import listener_windows

                if not arguments.environment_pipe:
                    raise RuntimeError('The detached listener requires its private command environment')
                environment = listener_windows.receive_environment(arguments.environment_pipe)
            result = worker(arguments.job.resolve(strict=True), environment)
        else:
            result = status(arguments.job.resolve(strict=True))
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result['status'] in ('unavailable', 'delivery-unknown') else 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({'status': 'unavailable', 'error': str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
