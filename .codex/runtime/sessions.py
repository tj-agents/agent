"""Codex's native sessions: transcript history (Python 3.9+)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from host import Host
import transcripts

HOST = Host.CODEX
TOOL_CALL_TYPES = ('function_call', 'custom_tool_call', 'local_shell_call')
ARGUMENT_KEYS = ('arguments', 'input', 'action')



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


def resume_command(session_id: str) -> str:
    return f'codex resume {session_id}'
