"""Claude's native sessions: transcripts, recovery, relaunch and exact close (Python 3.9+)."""

from __future__ import annotations

import os
from pathlib import Path
import re

from host import Host
import transcripts

HOST = Host.CLAUDE
FRONTEND_FROM_REGISTRATION = True
FRONTEND_STATE: tuple[str, ...] = ()
VARIADIC = {"--add-dir", "--mcp-config", "--tools", "--allowedTools", "--allowed-tools",
            "--disallowedTools", "--disallowed-tools"}
SINGLE = {"--settings", "--plugin-dir", "--setting-sources", "--permission-mode", "--model"}
FLAGS = {"--strict-mcp-config", "--restricted", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}
TOOL_RESTRICTIONS = {"--tools", "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools"}


def history_roots(root: Path) -> tuple[Path, ...]:
    return (root,)


def transcript_files(directory: Path):
    return directory.glob('*/*.jsonl')


def transcript_ids(path: Path) -> tuple[str, ...]:
    return (path.stem,)


def read_session(path: Path) -> dict | None:
    result = dict(session=None, cwd=None, branch=None, messages=[], tool_strings=[], last_activity_raw=None)
    for number, event in transcripts.records(path):
        kind = event.get('type')
        if event.get('isSidechain'):
            continue
        if kind in ('user', 'assistant'):
            result['session'] = event.get('sessionId') or result['session'] or path.stem
            result['cwd'] = event.get('cwd') or result['cwd']
            result['branch'] = event.get('gitBranch') or result['branch']
            timestamp = event.get('timestamp')
            if timestamp:
                result['last_activity_raw'] = timestamp
            message = event.get('message', {})
            content = message.get('content') if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'tool_use':
                        result['tool_strings'].extend(transcripts.iter_strings(block.get('input')))
            text = transcripts.text_content(content)
            if text:
                result['messages'].append((number, kind, text))
    return result


def header(path: Path) -> dict | None:
    for _number, event in transcripts.records(path):
        if event.get("type") in ("user", "assistant") and not event.get("isSidechain") and event.get("cwd"):
            return {"host": HOST.value, "session": event.get("sessionId") or path.stem, "cwd": event.get("cwd"),
                    "branch": event.get("gitBranch")}
    return None


def recoverable(item: dict) -> bool:
    return True


def resume_command(session_id: str) -> str:
    return f'claude --resume {session_id}'


def default_profile(profile: str) -> bool:
    return os.path.normcase(str(Path(profile).resolve())) == os.path.normcase(
        str((Path.home() / HOST.home_directory).resolve()))


def open_tab(cli, item: dict, directory, label: str, instruction: str, status_path: Path, *, model=None,
             native_arguments=(), dangerously_skip_permissions=False) -> None:
    # Claude reads ~/.claude.json only without CLAUDE_CONFIG_DIR; set to ~/.claude it starts first-run onboarding.
    if default_profile(item["profile"]):
        profile_environment = {"clear": (HOST.home_variable,)}
    else:
        profile_environment = {"force": {HOST.home_variable: item["profile"]}}
    arguments = [*native_arguments, "--resume", item["id"]]
    if dangerously_skip_permissions:
        arguments.append("--dangerously-skip-permissions")
    if model:
        arguments += ["--model", model]
    arguments.append(instruction)
    cli.open_claude_tab(directory, label, arguments, status_file=status_path, **profile_environment)


def prepare_frontend(arguments, project: Path, environment, executable=None):
    return list(arguments), dict(environment), None


def attach_frontend(ticket, pid: int) -> None:
    return None


def release_frontend(ticket) -> None:
    return None


def debug_arguments(arguments: list[str], directory: Path) -> tuple[list[str], str | None]:
    if any(value == "--debug-file" or value.startswith("--debug-file=") for value in arguments):
        return arguments, None
    debug_log = directory / "native-debug.log"
    return ["--debug-file", str(debug_log), *arguments], str(debug_log)


def native_arguments(argv: list[str]) -> list[str]:
    kept, index = [], 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            break
        name, equals, _ = token.partition("=")
        index += 1
        if name in FLAGS and not equals:
            kept.append(token)
        elif name in SINGLE | VARIADIC and equals:
            kept.append(token)
        elif name in SINGLE and index < len(argv):
            kept += [token, argv[index]]
            index += 1
        elif name in VARIADIC:
            values = []
            while index < len(argv) and not argv[index].startswith("-"):
                values.append(argv[index])
                index += 1
            if values:
                kept += [token, *values]
            elif name in TOOL_RESTRICTIONS:
                raise RuntimeError(f"Refusing recovery because {name} did not retain its tool restrictions")
    return kept


def session_identity() -> tuple[str, int] | None:
    session_id, pid = os.environ.get(HOST.session_variable), os.environ.get("CLAUDE_PID")
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return None
    return session_id, int(pid) if pid and pid.isdigit() else 0


def validate_relaunch(entry: dict, project: Path) -> None:
    identity = session_identity()
    if identity is not None and (identity[0] != entry["session_id"] or identity[1] and identity[1] != entry["pid"]):
        raise RuntimeError("the calling Claude session does not match its registered client")
    transcript = os.environ.get("CLAUDE_CODE_SESSION_LOG", "")
    if "subagents" in Path(transcript).parts:
        raise RuntimeError("a subagent cannot relaunch its parent client")


def check_relaunch(project: Path, executable: str, native: list[str], session_id: str, prompt: str) -> None:
    return None


def close_arguments(entry: dict) -> list[str]:
    return []


def own_entry(nearest, cwd: Path, session_id: str | None, shared) -> dict | None:
    return None


def _is_user(record: dict) -> bool:
    if (record.get("type") != "user" or record.get("isSidechain") or record.get("isMeta")
            or record.get("isCompactSummary")):
        return False
    content = (record.get("message") or {}).get("content")
    return isinstance(content, str) or (isinstance(content, list) and not any(
        isinstance(item, dict) and item.get("type") == "tool_result" for item in content))


def _is_assistant(record: dict) -> bool:
    return (record.get("type") == "assistant" and not record.get("isSidechain")
            and not record.get("isMeta") and not record.get("isCompactSummary"))


def user_turn(path: Path) -> str:
    token = None
    for _number, record in transcripts.records(path):
        if _is_user(record):
            candidate = record.get("uuid")
            token = candidate if isinstance(candidate, str) and candidate else None
    if token:
        return token
    raise ValueError("Claude transcript has no bounded, non-sidechain user turn")


def transcript(session_id: str, supplied: Path | None) -> Path:
    if supplied is not None:
        return supplied.resolve()
    root = HOST.history_root
    matches = list(root.glob("*/" + session_id + ".jsonl")) if root.is_dir() else []
    if len(matches) != 1:
        raise ValueError("exact Claude transcript is unavailable")
    return matches[0].resolve()


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def finished(state: dict, data: dict) -> bool:
    supplied = _text(data.get("transcript_path") or data.get("transcriptPath"))
    expected = _text(state.get("transcript"))
    token = _text(state.get("claude_user_uuid"))
    if not supplied or not expected or not token or Path(supplied).resolve() != Path(expected).resolve():
        return False
    latest_user = None
    complete = False
    for _number, record in transcripts.records(Path(expected)):
        if _is_user(record):
            candidate = record.get("uuid")
            latest_user = candidate if isinstance(candidate, str) and candidate else None
            complete = False
        elif latest_user == token and _is_assistant(record):
            message = record.get("message")
            complete = isinstance(message, dict) and message.get("stop_reason") == "end_turn"
    return latest_user == token and complete


def completes(state: dict, data: dict, turn_id: str | None) -> bool:
    return finished(state, data)


def verify_receipt(entry: dict, shared) -> None:
    provenance = entry.get("provenance")
    if (entry.get("host") != HOST.value or not isinstance(provenance, dict)
            or provenance.get("source") != "agent-startup-binding"
            or not isinstance(provenance.get("receipt"), str)):
        raise shared.Refusal("record is not the verified Claude startup binding")
    path = Path(provenance["receipt"]).resolve()
    if (shared.state_directory() / "recovery-launches").resolve() not in path.parents:
        raise shared.Refusal("startup binding receipt is outside private runtime state")
    receipt = shared.read_json(path) or {}
    wrapper = receipt.get("wrapper")
    frontend = {"pid": entry.get("pid"), "started_at": entry.get("pid_started_at")}
    if not (receipt.get("session") == entry.get("session_id")
            and receipt.get("profile") == entry.get("profile") and receipt.get("state") == "launched"
            and isinstance(wrapper, dict) and wrapper.get("child") == frontend
            and entry.get("frontend") == frontend and wrapper.get("terminal") == entry.get("terminal")):
        raise shared.Refusal("startup binding receipt does not prove this Claude frontend")


def verify_frontend(entry: dict, shared) -> None:
    verify_receipt(entry, shared)


def prepared_record(record: dict, session_id: str, shared) -> dict:
    verify_receipt(record, shared)
    return record


def close_target(session_id: str, supplied: Path | None) -> dict:
    path = transcript(session_id, supplied)
    return {"transcript": str(path), "claude_user_uuid": user_turn(path)}


def arm_close(entry: dict, turn_id: str | None, target: dict, supplied: Path | None, shared) -> dict:
    verify_receipt(entry, shared)
    activity = shared.active_turn_activity(entry)
    path = transcript(entry["session_id"], supplied or Path(target["transcript"]))
    if str(path) != target.get("transcript") or user_turn(path) != target.get("claude_user_uuid"):
        raise ValueError("Claude transcript or user changed after preparation")
    return {"working_activity": activity, "transcript": str(path), "claude_user_uuid": target["claude_user_uuid"]}


def close_monitor(request: dict, shared):
    return None


def settled(request: dict, monitor, completed: bool, shared):
    session = shared.session_activity(request["entry"])
    if completed:
        if session.turn_state != "awaiting_input" or session.last_activity_at <= request["armed_at"]:
            raise shared.Cancelled("completed turn is no longer awaiting input")
        return session.last_activity_at
    if session.last_activity_at != request["working_activity"] and session.turn_state != "awaiting_input":
        raise shared.Cancelled("the armed working turn changed")
    return None


def close_guard(request: dict, activity, completed: bool, shared) -> None:
    if not completed:
        raise shared.Cancelled("completion changed before close")
    if not finished(request, {"transcript_path": request["transcript"]}):
        raise shared.Cancelled("Claude input or final response changed before close")
    shared.awaiting_input_activity(request["entry"], activity)
