"""Codex's native sessions: transcripts, recovery, relaunch and exact close (Python 3.9+)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import agent_cli
from codex import client
from host import Host
import required_plugins
import transcripts

HOST = Host.CODEX
FRONTEND_FROM_REGISTRATION = False
FRONTEND_STATE = (client.RECORD_ENV, client.RECORD_PATH_ENV)
TOOL_CALL_TYPES = ('function_call', 'custom_tool_call', 'local_shell_call')
ARGUMENT_KEYS = ('arguments', 'input', 'action')
RELAUNCH_SINGLE = {"--config", "-c", "--model", "-m", "--profile", "-p", "--sandbox", "-s",
                   "--ask-for-approval", "-a", "--add-dir", "--enable", "--disable",
                   "--remote", "--remote-auth-token-env", "--local-provider"}
RELAUNCH_FLAGS = {"--no-daemon", "--no-alt-screen", "--search", "--oss", "--strict-config",
                  "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox"}
LAUNCHER_CONFIG = {*(f'shell_environment_policy.set.{name}=""' for name in (client.RECORD_ENV, client.RECORD_PATH_ENV)),
                   agent_cli.CODEX_KEEP_TAB_TITLE[1]}


def history_roots(root: Path) -> tuple[Path, ...]:
    return (root, root.parent / "archived_sessions")


def transcript_files(directory: Path):
    return directory.rglob('*.jsonl')


def transcript_ids(path: Path) -> tuple[str, ...]:
    return (path.stem, path.stem[-36:]) if len(path.stem) > 36 else (path.stem,)


def tool_strings(payload: dict):
    for key in ARGUMENT_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                yield value
                continue
        yield from transcripts.iter_strings(value)


def read_session(path: Path) -> dict | None:
    result = dict(session=None, cwd=None, branch=None, messages=[], tool_strings=[], last_activity_raw=None)
    for number, event in transcripts.records(path):
        kind = event.get('type')
        payload = event.get('payload', {})
        if not isinstance(payload, dict):
            continue
        if kind == 'session_meta':
            source = payload.get('source')
            if isinstance(source, dict) and 'subagent' in source:
                return None
            result['source'] = source
            result.update(session=payload.get('id'), cwd=payload.get('cwd'))
            result['branch'] = (payload.get('git') or {}).get('branch')
        elif kind == 'response_item' and payload.get('type') == 'message':
            role = payload.get('role')
            if role in ('user', 'assistant'):
                timestamp = payload.get('timestamp') or event.get('timestamp')
                if timestamp:
                    result['last_activity_raw'] = timestamp
                text = transcripts.text_content(payload.get('content'))
                if text:
                    result['messages'].append((number, role, text))
        elif kind == 'response_item' and payload.get('type') in TOOL_CALL_TYPES:
            result['tool_strings'].extend(tool_strings(payload))
    return result


def header(path: Path) -> dict | None:
    for _number, event in transcripts.records(path):
        payload = event.get("payload")
        if event.get("type") == "session_meta" and isinstance(payload, dict):
            source = payload.get("source")
            if isinstance(source, dict) and "subagent" in source:
                return None
            return {"host": HOST.value, "session": payload.get("id"), "cwd": payload.get("cwd"), "source": source,
                    "branch": (payload.get("git") or {}).get("branch")}
    return None


def recoverable(item: dict) -> bool:
    return item.get("source") == "cli"


def resume_command(session_id: str) -> str:
    return f'codex resume {session_id}'


def requires_verified_handoff(require_session_start: bool) -> bool:
    return require_session_start


def handoff_executable(target: str, project: Path, environment, *, allow_managed=True) -> str:
    prerequisite = 'Codex handoff requires an installed native Codex 0.166.0 or newer for paginated input verification'
    floor = agent_cli.parse_codex_version('0.166.0')

    def probe(path):
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ValueError('the runtime path is not absolute')
        candidate = candidate.resolve(strict=True)
        if not candidate.is_file():
            raise ValueError('the runtime path is not a file')
        if os.name == 'nt':
            if candidate.suffix.casefold() != '.exe':
                raise ValueError('the runtime path is not a native executable')
        else:
            with candidate.open('rb') as stream:
                if stream.read(2) == b'#!' or not os.access(candidate, os.X_OK):
                    raise ValueError('the runtime path is not a native executable')
        result = invoke(str(candidate), '--version')
        version = agent_cli.parse_codex_version(result)
        if version is None:
            raise ValueError('the runtime did not report a Codex version')
        return str(candidate), version

    def invoke(executable, *arguments):
        result = subprocess.run(
            [executable, *arguments], cwd=project, env=dict(environment),
            capture_output=True, text=True, encoding='utf-8', timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if result.returncode:
            raise ValueError(f'native {" ".join(arguments)} failed with exit {result.returncode}')
        return result.stdout.strip()

    try:
        selected, version = probe(target)
        if agent_cli.compare_codex_version(version, floor) >= 0:
            return selected
        if not allow_managed:
            raise ValueError(f'the selected runtime is {version["text"]}')
        metadata = json.loads(invoke(selected, 'app-server', 'daemon', 'version'))
        if (not isinstance(metadata, dict) or metadata.get('status') != 'running'
                or metadata.get('cliVersion') != version['text']
                or not isinstance(metadata.get('managedCodexPath'), str)
                or not isinstance(metadata.get('managedCodexVersion'), str)):
            raise ValueError('read-only daemon version metadata is missing or inconsistent')
        managed, installed = probe(metadata['managedCodexPath'])
        if installed['text'] != metadata['managedCodexVersion']:
            raise ValueError('the managed runtime version does not match read-only daemon metadata')
        if agent_cli.compare_codex_version(installed, floor) < 0:
            raise ValueError(f'the installed managed runtime is {installed["text"]}')
        return managed
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RuntimeError(f'{prerequisite}: {error}') from error


def validate_open(owned_backend: bool) -> None:
    return None


def recovery_receipt(status_path: Path, item: dict, owned_backend: bool) -> dict:
    if not owned_backend:
        return {}
    binding = client.launch_binding(status_path, item['id'], item['cwd'], item['profile'])
    return {'status_path': str(status_path.resolve()),
            'client_binding': {key: binding[key] for key in ('token', 'recipient', 'profile')}}


def verify_resume(executable: str) -> None:
    try:
        result = subprocess.run([executable, "resume", "--help"], text=True, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Could not verify native Codex resume support: {exc}") from None
    if result.returncode or "SESSION_ID" not in result.stdout + result.stderr:
        raise ValueError("The native Codex executable does not support exact `resume SESSION_ID [PROMPT]`.")


def open_tab(cli, item: dict, directory, label: str, instruction: str, status_path: Path, *, model=None,
             native_arguments=(), dangerously_skip_permissions=False, owned_backend=False) -> None:
    executable, _ = cli.resolve_codex_executable()
    verify_resume(executable)
    old_profile = os.environ.get(HOST.home_variable)
    os.environ[HOST.home_variable] = item["profile"]
    arguments = [*cli.CODEX_KEEP_TAB_TITLE, *native_arguments, "resume", item["id"]]
    if model:
        arguments += ["-m", model]
    arguments.append(instruction)
    try:
        cli.launch_tab(
            directory, sys.executable, label,
            cli.bound_terminal_arguments(HOST.value, directory, executable, arguments, status_path,
                                         require_session_start=owned_backend),
            clear=("TERM",),
            force={HOST.home_variable: item["profile"]},
        )
    finally:
        if old_profile is None:
            os.environ.pop(HOST.home_variable, None)
        else:
            os.environ[HOST.home_variable] = old_profile


def prepare_frontend(arguments, project: Path, environment, executable=None, *, handoff=False, hook_roots=None):
    return client.prepare(arguments, project, environment, executable=executable, handoff=handoff, hook_roots=hook_roots)


def attach_frontend(ticket, pid: int) -> None:
    client.attach(ticket, pid)


def release_frontend(ticket) -> None:
    client.release(ticket)


def handoff_recipient(ticket, register_session, terminal):
    if ticket is None or (ticket.get('backend') or {}).get('kind') != 'handoff':
        raise RuntimeError('Codex handoff has no owned native backend binding')
    table = register_session.process_table()
    lookup = register_session.build_process_lookup()
    def peer_state(pid):
        info = lookup(pid)
        return ('present', info) if info is not None else ('absent', None)
    try:
        record, submitted = client.bind_recipient(ticket, table, peer_state)
    except client.RecipientPending:
        return None
    recipient = record['recipient']
    store_module = register_session._load_session_store()
    if store_module is None:
        raise RuntimeError('Agent Store is unavailable for the verified handoff recipient')
    with store_module.SessionStore() as store:
        session = store.get_session(store_module.session_handle(HOST.value, recipient['thread_id']))
        if session is None or session.ended_at is not None:
            raise RuntimeError('Handoff SessionStart has no existing live Agent Store registration')
        if (not session.profile or client.comparable_path(Path(session.profile).resolve())
                != client.comparable_path(Path(record['profile']).resolve())):
            raise RuntimeError('Handoff Agent Store registration has a different profile')
    details = client_details({**record, **submitted})
    if terminal is not None:
        details['terminal'] = terminal
    entry = register_session.record({'session_id': recipient['thread_id'], 'cwd': recipient['cwd']}, client={
        'pid': record['frontend']['pid'], 'host': HOST.value,
        'pid_started_at': record['frontend']['started_at'], **details,
    }, require_existing=True)
    if entry is None:
        raise RuntimeError('Verified handoff recipient could not be attached to Agent Store')
    with store_module.SessionStore() as store:
        session = store.get_session(store_module.session_handle(HOST.value, recipient['thread_id']))
        if (session is None or session.ended_at is not None or session.pid != record['frontend']['pid']
                or session.pid_started_at != record['frontend']['started_at']
                or client.comparable_path(Path(session.cwd).resolve()) != client.comparable_path(Path(recipient['cwd']).resolve())
                or client.comparable_path(Path(session.profile).resolve()) != client.comparable_path(Path(record['profile']).resolve())):
            raise RuntimeError('Agent Store did not retain the exact verified handoff client')
    return {'token': record['token'], 'recipient': recipient, 'profile': record['profile']}


def debug_arguments(arguments: list[str], directory: Path) -> tuple[list[str], str | None]:
    return arguments, None


def native_arguments(argv: list[str], entry=None) -> list[str]:
    if entry is not None and (entry.get('backend') or {}).get('kind') == 'handoff':
        kept, index, endpoints = [], 0, 0
        while index < len(argv):
            token = argv[index]
            index += 1
            name, equals, value = token.partition('=')
            if name == '--remote':
                if not equals:
                    if index >= len(argv):
                        raise RuntimeError('Owned handoff endpoint is missing from its frontend')
                    value = argv[index]
                    index += 1
                endpoints += 1
                if (not value.startswith('unix://') or not Path(value[7:]).is_absolute()
                        or client.comparable_path(Path(value[7:]).resolve())
                        != client.comparable_path(Path(entry['backend']['socket_path']).resolve())):
                    raise RuntimeError('Owned handoff frontend selected a different endpoint')
            else:
                kept.append(token)
        if endpoints != 1:
            raise RuntimeError('Owned handoff frontend requires exactly one endpoint')
        argv = kept
    kept, index, selector, subcommand = [], 0, False, False
    while index < len(argv):
        token = argv[index]
        name, equals, _ = token.partition("=")
        index += 1
        if token == "--":
            break
        if not token.startswith("-"):
            if token in {"resume", "fork"} and not subcommand:
                subcommand, selector = True, True
                continue
            if selector:
                selector = False
                continue
            continue
        if len(token) > 2 and token[:2] in {"-c", "-m", "-p", "-s", "-a", "-C", "-i"}:
            name, equals = token[:2], True
        if name in RELAUNCH_FLAGS and not equals:
            kept.append(token)
        elif name in RELAUNCH_SINGLE and equals:
            value = token[2:].removeprefix("=") if name in {"-c", "-m", "-p", "-s", "-a"} else token.partition("=")[2]
            if name not in {"-c", "--config"} or value not in LAUNCHER_CONFIG:
                kept.append(token)
        elif name in RELAUNCH_SINGLE and index < len(argv):
            if name not in {"-c", "--config"} or argv[index] not in LAUNCHER_CONFIG:
                kept += [token, argv[index]]
            index += 1
        elif name in {"--cd", "-C", "--image", "-i"} and not equals:
            index += 1
        elif name == "--dangerously-bypass-hook-trust":
            raise RuntimeError("Refusing to carry a hook-trust bypass into recovery")
        elif name == "--last":
            selector = False
    return kept


def relaunch_backend(entry):
    if (entry.get('backend') or {}).get('kind') != 'handoff':
        return None
    record = json.loads(client.binding_path(entry['launch_token']).read_text(encoding='utf-8'))
    frozen = client.freeze_binding(record)
    if (frozen['frontend']['pid'] != entry['pid'] or frozen['backend'] != entry['backend']
            or frozen['recipient']['thread_id'] != entry['session_id']):
        raise RuntimeError('Owned refresh backend differs from its verified source client')
    return frozen


def backend_closed(binding, shared):
    if binding is None:
        return True
    for name in ('backend', 'supervisor'):
        if shared.identity_status(binding[name]['pid'], binding[name]['started_at']) != 'exited':
            return False
    path = client.binding_path(binding['token']).with_name('backend.json')
    try:
        receipt = json.loads(path.read_text(encoding='utf-8'))
        record = json.loads(path.with_name('client.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    if not isinstance(receipt, dict) or not isinstance(record, dict):
        return False
    if any(record.get(key) != value for key, value in binding.items() if key != 'frontend_message_id'):
        return False
    if (receipt.get('state') != 'exited' or receipt.get('token') != binding['token']
            or receipt.get('wrapper') != binding['wrapper'] or receipt.get('supervisor') != binding['supervisor']
            or receipt.get('backend') != binding['backend']):
        return False
    return not Path(binding['backend']['socket_path']).exists()


def validate_relaunch(entry: dict, project: Path) -> None:
    native_cwd = entry.get("native_cwd")
    if (not isinstance(native_cwd, str) or not Path(native_cwd).is_absolute()
            or Path(native_cwd).resolve() != project.resolve()):
        raise RuntimeError("the refresh checkout does not match this Codex thread's native checkout")


def check_relaunch(project: Path, executable: str, native: list[str], session_id: str, prompt: str) -> None:
    required_plugins.check_launch(project, HOST, executable, [*native, "resume", session_id, prompt])



def current_client(session_id: str, nearest, shared, *, project=None):
    info, record = client.recorded_client(session_id, nearest, shared.process_info,
                                          shared.register_session.process_table(), project=project,
                                          peer_state=shared.process_state)
    return info, client_details(record, shared.register_session)


def client_details(record, registration=None):
    return {
        "client_identity": "managed-input", "launch_cwd": record["launch_cwd"],
        "title": record.get("title"), "client_message_id": record["client_message_id"],
        "native_turn_id": record["turn_id"],
        "frontend_message_id": record.get("frontend_message_id", record["client_message_id"]),
        "launch_token": record.get("token"),
        "native_rollout": record["native_rollout"],
        "native_cwd": record.get("native_cwd"),
        "recording_path": record["recording_path"], "backend": record["backend"],
        "profile": record["profile"],
        "terminal": registration.terminal_identity(record.get("terminal_environment", {})) if registration else None,
    }


def own_entry(nearest, cwd: Path, session_id: str | None, shared) -> dict | None:
    if not session_id:
        session_id = os.environ.get(HOST.session_variable) or os.environ.get("CODEX_SESSION_ID")
        if not session_id:
            raise shared.Refusal("native Codex thread identity is unavailable; preserve the frontend and backend")
    try:
        info, details = current_client(session_id, nearest, shared)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        raise shared.Refusal(str(error)) from error
    return shared.register_session.record(
        {"session_id": session_id, "cwd": str(cwd)},
        client={"pid": info.pid, "host": HOST.value, "pid_started_at": info.started_at, **details},
    )


def revalidate_client(entry: dict, shared) -> None:
    nearest = shared.process_info(entry["pid"])
    if nearest is None or entry.get("client_identity") != "managed-input":
        raise shared.Refusal("the frozen native client binding is unavailable")
    info, details = current_client(entry["session_id"], nearest, shared, project=entry.get("cwd"))
    current = {**details, "host": HOST.value, "session_id": entry["session_id"],
               "pid": info.pid, "pid_started_at": info.started_at}
    if any(current.get(key) != entry.get(key) for key in shared.CLIENT_BINDING_FIELDS):
        raise shared.Refusal("the native input or frontend binding changed before close")


def verify_frontend(entry: dict, shared) -> None:
    revalidate_client(entry, shared)
    if (entry.get('backend') or {}).get('kind') == 'handoff':
        return
    context = client.process_context(entry["pid"])
    if (client.comparable_path(Path(context.get(client.RECORD_PATH_ENV) or ''))
            != client.comparable_path(Path(entry.get("recording_path") or ''))
            or context.get(client.RECORD_ENV) not in ("1", "true", "TRUE", "yes", "YES")
            or Path(context["profile"]) != Path(entry.get("profile"))
            or (entry.get("backend") is not None and client.daemon_identity(
                {HOST.home_variable: entry["profile"]}, expected_pid=entry["backend"]["pid"],
            ) != entry["backend"])
            or client.latest_input(entry["recording_path"]) != entry.get("frontend_message_id", entry.get("client_message_id"))):
        raise shared.Refusal("the frontend input or backend binding changed before close")


def completes(state: dict, data: dict, turn_id: str | None) -> bool:
    return bool(turn_id) and state.get("turn_id") == turn_id


def prepared_record(record: dict, session_id: str, shared) -> dict:
    return shared.own_host_and_entry(session_id=session_id)


def close_target(session_id: str, supplied: Path | None) -> dict:
    return {}


def arm_close(entry: dict, turn_id: str | None, target: dict, supplied: Path | None, shared) -> dict:
    if turn_id != entry.get("native_turn_id"):
        raise ValueError("Codex arming requires the current native turn")
    revalidate_client(entry, shared)
    completion = client.NativeCompletion(entry)
    completion.poll()
    return {"working_activity": None, "native_completion": completion.pin()}


def close_monitor(request: dict, shared):
    pin = request.get("native_completion")
    if not isinstance(pin, dict):
        raise shared.Refusal("the native close binding is missing")
    return client.NativeCompletion(request["entry"], pin)


def settled(request: dict, monitor, completed: bool, shared):
    try:
        return monitor if monitor.poll() else None
    except RuntimeError as error:
        raise shared.Cancelled(str(error)) from error


def close_guard(request: dict, activity, completed: bool, shared) -> None:
    try:
        finished = activity.poll()
    except RuntimeError as error:
        raise shared.Cancelled(str(error)) from error
    if not finished:
        raise shared.Cancelled("the bound native turn is no longer complete")
