"""Shared launch primitives for the native agent CLIs, on Windows and POSIX.

Every agent tab runs under `tj run`, opened in Windows Terminal or tmux; a launch is confirmed when
`tj run` records it in agent-store.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from typing import TYPE_CHECKING, cast

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from tj_agent.host import Host
from launch_contract import Launch
from launch_errors import LaunchError, LaunchTimeout
from package_root import find_package_root
import agent_store

if TYPE_CHECKING:
    from claude import launch as claude_launch
    from codex import launch as codex_launch

    _claude_implementation: Launch = claude_launch
    _codex_implementation: Launch = codex_launch

_HOST_LAUNCHES: dict[Host, Launch] = {}
IS_WINDOWS = os.name == 'nt'


def host_launch(host: Host) -> Launch:
    if host not in _HOST_LAUNCHES:
        root = find_package_root(HERE)
        native = root / f'.{host.value}' / 'runtime' / 'launch.py'
        path = native if (root / '.git').exists() else HERE / host.value / 'launch.py'
        if not path.is_file():
            raise LaunchError(f'The packaged {host.value} launch adapter was not found under {root}.')
        spec = importlib.util.spec_from_file_location(f'agent_{host.value}_launch', path)
        if spec is None or spec.loader is None:
            raise LaunchError(f'The {host.value} launch adapter cannot be loaded from {path}.')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _HOST_LAUNCHES[host] = cast(Launch, module)
    return _HOST_LAUNCHES[host]


def make_stdio_encoding_lossy():
    """Replace an unencodable character in a print rather than crashing it.

    A console code page (Windows cp1252) or a redirected pipe can reject a character in a title or path
    that the terminal itself displayed fine. Called once at the top of a launcher's `main`, before any
    print: without it, a success message for a launch that already happened can raise UnicodeEncodeError
    and get reported as a failure.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')


def launch_environment(clear=(), force=None):
    """The environment changes a launched CLI needs: names to remove and values to set.

    Returned rather than applied, because on POSIX they travel inside the launched command line: a tab
    handler starts the command from an already-running terminal process that never sees ours.
    """
    session_state = (name for host in Host for name in host_launch(host).SESSION_ENVIRONMENT)
    cleared = list(dict.fromkeys((*session_state, 'WORKBOARD_LAUNCH_TOKEN', 'WORKBOARD_WORKFLOW_TOKEN', *clear)))
    forced = dict(force or {})
    return [name for name in cleared if name not in forced], forced


def prompt_file_argument(path):
    """(sentence, resolved_path) for a prepared prompt file: the 'Read the file at ...' sentence and the
    same resolved Path, so a caller never re-resolves it for its own messages.

    Shared by the Agent launch and recovery paths so the wording and file check have one owner.
    """
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise LaunchError(f'Prompt path is not a file: {resolved}')
    return f'Read the file at {resolved} and follow its instructions, working from the current directory.', resolved


def resolve_tab_directory(path):
    """The absolute-but-not-resolved working directory for a tab, validated to exist.

    Not .resolve(): on Windows that rewrites a mapped or subst drive to its target, and native history keys a
    session's history by the directory string it was started in. Called by both Python launchers first,
    before any filesystem or lane-table work, so a bad directory is reported before anything else they
    check. The error always names the
    absolute path.
    """
    directory = Path(os.path.abspath(path))
    if not directory.is_dir():
        raise LaunchError(f'Working directory is not a directory: {directory}')
    return directory


def report_launch_failure(exc, err=None):
    """Print a LaunchError (or LaunchTimeout) to `err` and return the exit code a launcher's `main` should
    use: 3 for a LaunchTimeout, because the terminal control command itself timed out and the tab may
    already have opened -- distinct from every other failure (1), where the launch did not happen.

    `err` defaults to `None` rather than to `sys.stderr` directly: a default parameter is bound once, when
    this function is defined, to whatever `sys.stderr` was at that moment -- not re-read at call time. A
    caller that redirects `sys.stderr` after this module loads (every test here does) would otherwise
    print to a stream nobody is capturing.
    """
    if err is None:
        err = sys.stderr
    print(str(exc), file=err)
    if isinstance(exc, LaunchTimeout):
        print('The tab may already have opened; check the terminal before launching another.', file=err)
        return 3
    return 1


def _lanes_module():
    root_helper = HERE / "package_root.py"
    spec = importlib.util.spec_from_file_location("agent_cli_package_root", root_helper)
    package_root = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package_root)
    for root in HERE.parents:
        if package_root.is_package_root(root):
            path = root / '.agents' / 'agent' / 'resources' / 'lanes' / 'resolve.py'
            if path.is_file():
                break
            raise LaunchError(f'The packaged lane resolver was not found under {root}.')
    else:
        raise LaunchError(f'The package root was not found relative to {HERE}.')
    spec = importlib.util.spec_from_file_location('agent_cli_lanes', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Lane -> model for a chosen harness, from the canonical lane tables, the repo's only model-name owner.
# Resolution never picks the lane: a caller that supplies neither a lane nor a model gets the CLI's own
# configured default, because guessing a lane from a prompt is how an expensive model ends up serving a
# rename. frontier resolves the tier above the ladder, which no lane resolves to: its selection is the
# user's explicit request, never task shape.
def resolve_lane(host, lane=None, frontier=False):
    if not lane and not frontier:
        raise LaunchError('resolve_lane needs a lane or frontier.')
    if lane and frontier:
        raise LaunchError('A lane and the frontier tier are mutually exclusive.')
    lanes = _lanes_module()
    try:
        entry = lanes.frontier(host) if frontier else lanes.resolve(host, lane)
    except SystemExit as exc:
        raise LaunchError(str(exc)) from None
    return entry


def resolve_lane_model(host, lane=None, frontier=False):
    """(model, effort) for a lane or the frontier tier; effort is None when the table prices none."""
    entry = resolve_lane(host, lane=lane, frontier=frontier)
    return entry['model'], entry.get('effort')


# Windows Terminal mangles argument values on the way to the tab process, and does it silently. It splits
# its own command line on `;` into subcommands, so an unescaped semicolon in a prompt truncates the prompt
# and turns the remainder into a bogus program; the tab still opens, which is what made the loss invisible.
# It then re-joins the remaining args into one child command line, quoting an arg only when that arg
# contains a space and escaping nothing inside it, and the tab process re-parses that string by
# CommandLineToArgvW rules. So a quote is eaten, and a trailing backslash in a spaced value escapes the
# closing quote and swallows the next argument whole.
def terminal_argument(value):
    # Windows Terminal quotes on a space and nothing else, so a line break or tab in a value with no space
    # is dropped outright and no escape can carry it. A refused launch beats a silently shortened prompt.
    if re.search(r'[\r\n\t]', value) and ' ' not in value:
        raise LaunchError(
            f'Windows Terminal cannot carry a line break or tab in an argument containing no space, and would drop it: {value}'
        )

    # An empty value has no space to be quoted for, so Windows Terminal drops it and every later argument
    # shifts into its place.
    if not value:
        raise LaunchError('Windows Terminal cannot carry an empty argument, and would shift the next one into its place.')

    quoted = ' ' in value
    parts = []
    backslashes = 0
    for character in value:
        if character == '\\':
            backslashes += 1
            continue
        if character == '"':
            parts.append('\\' * (2 * backslashes + 1) + '"')
        else:
            parts.append('\\' * backslashes + character)
        backslashes = 0
    parts.append('\\' * (2 * backslashes if quoted else backslashes))

    # Escaped last, because Windows Terminal strips exactly one backslash before a semicolon, so this must
    # be the outermost layer for a value that legitimately ends a run of backslashes at a semicolon.
    return ''.join(parts).replace(';', '\\;')


def posix_inner_command(directory, cleared, forced, executable, arguments):
    """The argv a POSIX tab handler starts: change into <directory>, scrub the environment, exec.

    Never a shell string built from the caller's values -- each piece travels as its own argv element, so a
    prompt with quotes, `;`, `$`, backslashes or a newline reaches the executable unparsed by any shell.
    """
    env_args = []
    for name in cleared:
        env_args += ['-u', name]
    env_args += [f'{name}={value}' for name, value in forced.items()]
    return ['sh', '-c', 'cd "$1" && shift && exec "$@"', 'sh', str(directory), 'env', *env_args, executable, *arguments]


def _client(run, command, what, **kwargs):
    """Run a terminal's own control command; every way it can fail is a LaunchError naming `what`."""
    try:
        result = run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, **kwargs)
    except subprocess.TimeoutExpired:
        raise LaunchTimeout(f'{what} did not answer within 30 seconds; it may still have taken effect.') from None
    except OSError as exc:
        raise LaunchError(f'{what} could not be run: {exc}') from None
    if result.returncode != 0:
        raise LaunchError(f'{what} failed (exit {result.returncode}): {(result.stderr or result.stdout).strip()}')
    return result.stdout.strip()


def _environment_for(environ, cleared, forced):
    env = dict(environ)
    for name in cleared:
        env.pop(name, None)
    env.update(forced)
    return env


TJ_RUN_TIMEOUT_SECONDS = 30
SESSION_TIMEOUT_SECONDS = 180


def _launch_tmux(directory, title, command, cleared, forced, environ, run):
    inner = posix_inner_command(directory, cleared, forced, command[0], command[1:])
    window = _client(run, ['tmux', 'new-window', '-n', title, '-P', '-F', '#{window_id}', '--', *inner],
                     'tmux new-window')
    if window:
        _client(run, ['tmux', 'set-option', '-w', '-t', window, 'automatic-rename', 'off'], 'tmux set-option')
        _client(run, ['tmux', 'set-option', '-w', '-t', window, 'allow-rename', 'off'], 'tmux set-option')


def _launch_windows_terminal(directory, title, command, cleared, forced, environ, run):
    terminal = shutil.which('wt.exe')
    if not terminal:
        raise LaunchError('wt.exe was not found on PATH.')
    # --window 0 is the most recently focused window; omitting it opens a separate OS window.
    arguments = ['--window', '0', 'new-tab', '--startingDirectory', str(directory), '--title', title,
                 '--suppressApplicationTitle', *command]
    _client(run, [terminal, *(terminal_argument(value) for value in arguments)], 'Windows Terminal',
            env=_environment_for(environ, cleared, forced))


_REAL_RUN = subprocess.run


def open_tab(directory, title, command, cleared=(), forced=None, environ=os.environ, run=_REAL_RUN):
    """Open `command` as a new tab titled `title`, in Windows Terminal or the current tmux server."""
    if run is _REAL_RUN and 'PYTEST_CURRENT_TEST' in os.environ:
        raise LaunchError('Refusing to open a real terminal tab under pytest; inject a fake run.')
    forced = dict(forced or {})
    if IS_WINDOWS:
        return _launch_windows_terminal(directory, title, command, cleared, forced, environ, run)
    if environ.get('TMUX'):
        return _launch_tmux(directory, title, command, cleared, forced, environ, run)
    raise LaunchError('Agent tabs open in Windows Terminal or tmux; start tmux first.')


def await_launch(launch_id, want_session=False, timeout=TJ_RUN_TIMEOUT_SECONDS,
                 session_timeout=SESSION_TIMEOUT_SECONDS, clock=time.monotonic, wait=time.sleep):
    """The `show --launch-id` data once tj run records the launch, and its session when asked for."""
    start = clock()
    while True:
        try:
            shown = agent_store.run('show', '--launch-id', launch_id)
        except agent_store.AgentStoreError as error:
            if error.code != 'not-found':
                raise LaunchError(f'agent-store could not read launch {launch_id}: {error}') from error
            if clock() - start >= timeout:
                raise LaunchTimeout(f'tj run did not record launch {launch_id} within {timeout} seconds; '
                                    'check the new tab before launching again.') from None
        else:
            if not want_session or shown.get('session'):
                return shown
            if shown['launch']['state'] == 'ended':
                raise LaunchError(f'the agent in launch {launch_id} exited before its session started')
            if clock() - start >= session_timeout:
                raise LaunchTimeout(f'the agent in launch {launch_id} did not start a session within '
                                    f'{session_timeout} seconds; check its tab.')
        wait(0.5)


def launch(host, directory, title, arguments, item=None, parent=None, want_session=False,
           environ=os.environ, run=subprocess.run):
    """Open a `tj run` tab for one agent and return its confirmed launch."""
    adapter = host_launch(host)
    tj = shutil.which('tj')
    if not tj:
        raise LaunchError('tj is not on PATH; run the Agent install.')
    launch_id = uuid.uuid4().hex
    clear = [name for name in adapter.TAB_CLEARED if IS_WINDOWS or name != 'TERM']
    force = {name: value for name, value in adapter.TAB_FORCED.items() if IS_WINDOWS or name != 'TERM'}
    cleared, forced = launch_environment(clear=clear, force=force)
    command = [tj, 'run', '--host', str(host), '--title', title, '--cwd', str(directory),
               *(['--item', item] if item else []), '--launch-id', launch_id,
               *(['--parent', parent] if parent else []), '--', adapter.executable(), *arguments]
    open_tab(directory, title, command, cleared, forced, environ, run)
    return await_launch(launch_id, want_session=want_session)


def parent_session(value):
    """The parent's `host:id`: as given, the calling session for `current`, or None without a parent."""
    if value != 'current':
        return value
    for host in Host:
        if session_id := os.environ.get(host.session_variable):
            return f'{host}:{session_id}'
    raise LaunchError('bare --parent names the calling session, but this is not a Claude or Codex session.')


def parent_rule(parent):
    """The standing instruction a child gets to report back to the session that started it."""
    if not parent:
        return ''
    try:
        title = agent_store.run('show', parent)['session'].get('title')
    except agent_store.AgentStoreError:
        title = None
    name = f'"{title}" ({parent})' if title else parent
    return (f' Standing rule: the session {name} started you. Message it with agent:message when you finish, '
            "when you are blocked, and whenever Tommy's approval is needed, without waiting to be asked.")
