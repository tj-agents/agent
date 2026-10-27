"""Read selected local Codex or Claude history; never writes or uploads transcripts."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from host import Host
from host_sessions import SESSIONS


def _normalize_sep(value):
    text = str(value).replace('\\', '/')
    text = re.sub('/+', '/', text)
    if len(text) > 1:
        text = text.rstrip('/')
    return text


def normalize_for_match(value):
    """Collapse separators and fold case on Windows so path tokens compare equal across spellings."""
    text = _normalize_sep(value)
    return text.casefold() if os.name == 'nt' else text


_DRIVE_PATH = re.compile(r'^([a-z]):(/.*)?$')


def _path_variants(worktree_norm):
    """Every normalized spelling a session could record or run a command against.

    A Windows drive path also appears as Git Bash's MSYS form (``/c/...``) and WSL's
    ``/mnt/c/...``; both are how Claude's Bash tool and Codex shell calls write it.
    """
    variants = [worktree_norm]
    match = _DRIVE_PATH.match(worktree_norm) if os.name == 'nt' else None
    if match:
        drive, rest = match.group(1), match.group(2) or ''
        variants.append(f'/{drive}{rest}')
        variants.append(f'/mnt/{drive}{rest}')
    return variants


def _worktree_variants(worktree):
    """Variants of the path as given and as resolved; sessions record whichever spelling their shell used
    (8.3 short names, junctions, subst or mapped drives)."""
    path = Path(worktree).expanduser()
    spellings = (normalize_for_match(os.path.abspath(path)), normalize_for_match(path.resolve()))
    return list(dict.fromkeys(variant for spelling in spellings for variant in _path_variants(spelling)))


def _cwd_under(cwd, worktree_norms):
    if not cwd:
        return False
    norm = normalize_for_match(cwd)
    return any(norm == variant or norm.startswith(variant + '/') for variant in worktree_norms)


_PATH_CONTINUATION = r'[A-Za-z0-9_.-]'


def _contains_path_token(text, worktree_norms):
    if not text:
        return False
    norm = normalize_for_match(text)
    for variant in worktree_norms:
        pattern = (r'(?<!' + _PATH_CONTINUATION + r')' + re.escape(variant)
                   + r'(?!' + _PATH_CONTINUATION + r')')
        if re.search(pattern, norm):
            return True
    return False


def _parse_iso(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def session(path, host):
    sessions = SESSIONS[host]
    result = sessions.read_session(path)
    if result is None or not result['session'] or not result['messages']:
        return None
    # Native IDs are opaque identifiers, never shell fragments.
    if not re.fullmatch(r'[A-Za-z0-9_-]+', result['session']):
        return None
    result['host'] = host.value
    result['modified'] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    result['last_activity'] = result.pop('last_activity_raw') or result['modified']
    result['resume'] = sessions.resume_command(result['session'])
    return result


def query(root, host, *, cwd=None, project=None, worktree=None, count=5, pattern=None, regex=False,
          show_lines=False):
    if not root.is_dir():
        raise ValueError(f'No {host} history directory at {root}; select the correct profile or --history-root.')
    if count < 1:
        raise ValueError('--count must be positive')
    expression = re.compile(pattern if regex else re.escape(pattern), re.I) if pattern is not None else None
    worktree_norms = _worktree_variants(worktree) if worktree else None
    files = SESSIONS[host].transcript_files(root)
    results = []
    for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
        item = session(path, host)
        if not item:
            continue
        tool_strings = item.pop('tool_strings')
        if worktree_norms:
            if _cwd_under(item['cwd'], worktree_norms):
                item['matched_by'] = 'cwd'
            elif any(_contains_path_token(text, worktree_norms) for text in tool_strings):
                item['matched_by'] = 'tool'
            else:
                continue
        else:
            if cwd and (not item['cwd'] or Path(item['cwd']).resolve() != Path(cwd).resolve()):
                continue
            if project and project.casefold() not in (item['cwd'] or path.parent.name).casefold():
                continue
        messages = item.pop('messages')
        if expression:
            hits = [(line, text) for line, _, text in messages if expression.search(text)]
            if not hits:
                continue
            item['hits'] = len(hits)
            if show_lines:
                item['matches'] = [dict(line=line, text=text) for line, text in hits]
        item['message_count'] = len(messages)
        item['preview'] = next((re.sub(r'\s+', ' ', text)[:160] for _, role, text in messages
                                if role == 'user' and not text.lstrip().startswith('<')), '(no user preview)')
        results.append(item)
        if not worktree_norms and len(results) >= count:
            break
    if worktree_norms:
        results.sort(key=lambda it: _parse_iso(it['last_activity']), reverse=True)
        results = results[:count]
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('recent', 'search'))
    parser.add_argument('--host', type=Host, choices=list(Host), required=True)
    parser.add_argument('--history-root', type=Path, help='Override the transcript data directory, not a helper path')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--cwd', type=Path)
    selection.add_argument('--project')
    selection.add_argument('--all-projects', action='store_true')
    selection.add_argument('--worktree', type=Path, help='Match a recorded cwd or any tool-call input under this directory')
    parser.add_argument('--count', type=int, default=5)
    parser.add_argument('--pattern')
    parser.add_argument('--regex', action='store_true')
    parser.add_argument('--show-lines', action='store_true')
    args = parser.parse_args()
    if args.operation == 'search' and args.pattern is None:
        parser.error('search requires --pattern')
    cwd = args.cwd
    if args.operation == 'recent' and not (cwd or args.project or args.all_projects or args.worktree):
        cwd = Path.cwd()
    try:
        result = query(args.history_root or args.host.history_root, args.host, cwd=cwd,
                       project=args.project, worktree=args.worktree, count=args.count,
                       pattern=args.pattern, regex=args.regex, show_lines=args.show_lines)
    except (OSError, ValueError, re.error) as error:
        print(f'History query failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
