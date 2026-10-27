"""Codex's native sessions: transcripts, recovery, relaunch and exact close (Python 3.9+)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

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
RECORDER = {f'shell_environment_policy.set.{name}=""' for name in (client.RECORD_ENV, client.RECORD_PATH_ENV)}


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


def verify_resume(executable: str) -> None:
    try:
        result = subprocess.run([executable, "resume", "--help"], text=True, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Could not verify native Codex resume support: {exc}") from None
    if result.returncode or "SESSION_ID" not in result.stdout + result.stderr:
        raise ValueError("The native Codex executable does not support exact `resume SESSION_ID [PROMPT]`.")


def open_tab(cli, item: dict, directory, label: str, instruction: str, status_path: Path, *, model=None,
             native_arguments=(), dangerously_skip_permissions=False) -> None:
    executable, _ = cli.resolve_codex_executable()
    verify_resume(executable)
    old_profile = os.environ.get(HOST.home_variable)
    os.environ[HOST.home_variable] = item["profile"]
    arguments = [*native_arguments, "resume", item["id"]]
    if model:
        arguments += ["-m", model]
    arguments.append(instruction)
    try:
        cli.launch_tab(
            directory, sys.executable, label,
            cli.bound_terminal_arguments(HOST.value, directory, executable, arguments, status_path),
            clear=("TERM",),
            force={HOST.home_variable: item["profile"]},
        )
    finally:
        if old_profile is None:
            os.environ.pop(HOST.home_variable, None)
        else:
            os.environ[HOST.home_variable] = old_profile


def prepare_frontend(arguments, project: Path, environment, executable=None):
    return client.prepare(arguments, project, environment, executable=executable)


def attach_frontend(ticket, pid: int) -> None:
    client.attach(ticket, pid)


def release_frontend(ticket) -> None:
    client.release(ticket)


def debug_arguments(arguments: list[str], directory: Path) -> tuple[list[str], str | None]:
    return arguments, None


def native_arguments(argv: list[str]) -> list[str]:
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
            if name not in {"-c", "--config"} or value not in RECORDER:
                kept.append(token)
        elif name in RELAUNCH_SINGLE and index < len(argv):
            if name not in {"-c", "--config"} or argv[index] not in RECORDER:
                kept += [token, argv[index]]
            index += 1
        elif name in {"--cd", "-C", "--image", "-i"} and not equals:
            index += 1
        elif name == "--dangerously-bypass-hook-trust":
            raise RuntimeError("Refusing to carry a hook-trust bypass into recovery")
        elif name == "--last":
            selector = False
    return kept


def validate_relaunch(entry: dict, project: Path) -> None:
    native_cwd = entry.get("native_cwd")
    if (not isinstance(native_cwd, str) or not Path(native_cwd).is_absolute()
            or Path(native_cwd).resolve() != project.resolve()):
        raise RuntimeError("the refresh checkout does not match this Codex thread's native checkout")


def check_relaunch(project: Path, executable: str, native: list[str], session_id: str, prompt: str) -> None:
    required_plugins.check_launch(project, HOST, executable, [*native, "resume", session_id, prompt])


def close_arguments(entry: dict) -> list[str]:
    return ["--turn-id", entry["native_turn_id"]]


def current_client(session_id: str, nearest, shared, *, project=None):
    info, record = client.recorded_client(session_id, nearest, shared.process_info,
                                          shared.register_session.process_table(), project=project,
                                          peer_state=shared.process_state)
    return info, {
        "client_identity": "managed-input", "launch_cwd": record["launch_cwd"],
        "title": record.get("title"), "client_message_id": record["client_message_id"],
        "native_turn_id": record["turn_id"],
        "native_rollout": record["native_rollout"],
        "native_cwd": record.get("native_cwd"),
        "recording_path": record["recording_path"], "backend": record["backend"],
        "profile": record["profile"],
        "terminal": shared.register_session.terminal_identity(record.get("terminal_environment", {})),
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
    context = client.process_context(entry["pid"])
    if (context.get(client.RECORD_PATH_ENV) != entry.get("recording_path")
            or context.get(client.RECORD_ENV) not in ("1", "true", "TRUE", "yes", "YES")
            or Path(context["profile"]) != Path(entry.get("profile"))
            or (entry.get("backend") is not None and client.daemon_identity(
                {HOST.home_variable: entry["profile"]}, expected_pid=entry["backend"]["pid"],
            ) != entry["backend"])
            or client.latest_input(entry["recording_path"]) != entry.get("client_message_id")):
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
