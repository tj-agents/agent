"""Claude's native sessions: transcript history (Python 3.9+)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from tj_agent.host import Host
import transcripts

HOST = Host.CLAUDE


def history_roots(root: Path) -> tuple[Path, ...]:
    return (root,)


def transcript_files(directory: Path) -> Iterator[Path]:
    return directory.glob('*/*.jsonl')


def transcript_ids(path: Path) -> tuple[str, ...]:
    return (path.stem,)


def transcript_modified(session_id: str) -> float:
    paths = [
        path
        for path in transcript_files(HOST.history_root)
        if session_id in transcript_ids(path)
    ]
    if not paths:
        raise FileNotFoundError(f"Claude transcript not found for {session_id}")
    return max(path.stat().st_mtime for path in paths)


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


def resume_command(session_id: str) -> str:
    return f'claude --resume {session_id}'
