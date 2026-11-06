"""Shared launch primitives for the native agent CLIs, on Windows and POSIX.

Terminal launch, environment application, lane lookup and startup receipt mechanics shared by both hosts.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import TYPE_CHECKING, Literal, cast, overload

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from host import Host
from launch_contract import Launch
from launch_errors import LaunchError, LaunchTimeout
from package_root import find_package_root

if TYPE_CHECKING:
    from claude.launch import Adapter as ClaudeLaunch
    from codex.launch import Adapter as CodexLaunch

_HOST_LAUNCHES: dict[Host, Launch] = {}
IS_WINDOWS = os.name == 'nt'


@overload
def host_launch(host: Literal['codex', Host.CODEX]) -> CodexLaunch: ...


@overload
def host_launch(host: Literal['claude', Host.CLAUDE]) -> ClaudeLaunch: ...


@overload
def host_launch(host: Host | str) -> Launch: ...


def host_launch(host: Host | str) -> Launch:
    kind = Host(host)
    if kind not in _HOST_LAUNCHES:
        root = find_package_root(HERE)
        native = root / f'.{kind.value}' / 'runtime' / 'launch.py'
        path = native if (root / '.git').exists() else HERE / kind.value / 'launch.py'
        if not path.is_file():
            raise LaunchError(f'The packaged {kind.value} launch adapter was not found under {root}.')
        spec = importlib.util.spec_from_file_location(f'agent_{kind.value}_launch', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _HOST_LAUNCHES[kind] = cast(Launch, module)
    return _HOST_LAUNCHES[kind]


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
    check; launch_tab calls it again for callers that reach it directly. The error always names the
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
def resolve_lane(harness, lane=None, frontier=False):
    if not lane and not frontier:
        raise LaunchError('resolve_lane needs a lane or frontier.')
    if lane and frontier:
        raise LaunchError('A lane and the frontier tier are mutually exclusive.')
    lanes = _lanes_module()
    try:
        entry = lanes.frontier(harness) if frontier else lanes.resolve(harness, lane)
    except SystemExit as exc:
        raise LaunchError(str(exc)) from None
    return entry


def resolve_lane_model(harness, lane=None, frontier=False):
    """(model, effort) for a lane or the frontier tier; effort is None when the table prices none."""
    entry = resolve_lane(harness, lane=lane, frontier=frontier)
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


def _launch_tmux(directory, executable, title, arguments, cleared, forced, environ, run, popen):
    inner = posix_inner_command(directory, cleared, forced, executable, arguments)
    # Passing several argv elements after `--` is what makes tmux 3.0+ exec them directly without an
    # intermediate shell; a single joined string would be re-split by tmux's own command parser. No `-d`:
    # the new window comes to the front, as a new tab does in every other terminal.
    pane = _client(run, ['tmux', 'new-window', '-n', title, '-P', '-F', '#{pane_id}', '--', *inner], 'tmux new-window')
    # The session is already running in that window, so a failure past this point must not read as a
    # failed launch: a caller would retry and start a second writer.
    try:
        if not pane:
            raise LaunchError('tmux new-window did not report the new pane id')
        _client(run, ['tmux', 'select-pane', '-t', pane, '-T', title], 'tmux select-pane')
    except LaunchError as exc:
        print(f'warning: the session started, but its tmux pane title was not set to {title!r}: {exc}', file=sys.stderr)


def _launch_kitty(directory, executable, title, arguments, cleared, forced, environ, run, popen):
    listen_on = environ.get('KITTY_LISTEN_ON')
    if not listen_on:
        raise LaunchError(
            'kitty remote control is not reachable: KITTY_LISTEN_ON is not set. Add '
            "'allow_remote_control socket-only' and 'listen_on unix:/tmp/kitty-{kitty_pid}' to kitty.conf "
            'and restart kitty.'
        )
    window_id = environ.get('KITTY_WINDOW_ID', '')
    inner = posix_inner_command(directory, cleared, forced, executable, arguments)
    # For `launch`, --match selects a tab, and `id:` is a tab id; `window_id:` selects the tab holding the
    # caller's own window. The two id counters are independent, so `id:` lands in the wrong place.
    _client(run, ['kitty', '@', '--to', listen_on, 'launch', '--type=tab', '--match', f'window_id:{window_id}',
                  '--tab-title', title, '--title', title, *inner], 'kitty @ launch')


def _konsole_qdbus():
    return shutil.which('qdbus6') or shutil.which('qdbus')


def _launch_konsole(directory, executable, title, arguments, cleared, forced, environ, run, popen):
    qdbus = _konsole_qdbus()
    if not qdbus:
        raise LaunchError('Neither qdbus6 nor qdbus was found on PATH to drive Konsole over D-Bus.')
    service = environ.get('KONSOLE_DBUS_SERVICE')
    window = environ.get('KONSOLE_DBUS_WINDOW')
    if not service or not window:
        raise LaunchError('KONSOLE_DBUS_SERVICE or KONSOLE_DBUS_WINDOW is not set.')
    # The title becomes Konsole's tab-title format, where `%` sequences expand to the directory, program
    # and so on, with no escape for a literal `%`. The visible title must equal AGENT_CLI_TAB_TITLE, which
    # peer-cli resolves sessions by, so a `%` is refused rather than shown differently.
    if '%' in title:
        raise LaunchError(f'Konsole would expand the % in the tab title {title!r}; choose a title without %.')

    def call(*args):
        return _client(run, [qdbus, service, *args], f'{qdbus} {args[-1] if len(args) < 3 else args[1]}')

    try:
        session = call(window, 'newSession')
    except LaunchTimeout as exc:
        # No session id came back, so there is nothing here to close by typing into it; the D-Bus call may
        # still have created an empty, untitled tab despite the timeout, and this is the only way to say
        # so. A plain LaunchError, not a LaunchTimeout: there is no inner command running yet to disturb.
        raise LaunchError(f'{exc} An empty Konsole tab may have been left open; check for one before retrying.') from None

    session_path = f'/Sessions/{session}'
    script_path = None

    def abandon():
        """Try to end the empty tab by closing its shell; report whether that itself succeeded."""
        if script_path is not None:
            shutil.rmtree(script_path.parent, ignore_errors=True)
        # Konsole exposes no D-Bus call to close a session; ending its shell closes the tab this created.
        try:
            result = run([qdbus, service, session_path, 'sendText', 'exit\n'],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    try:
        # setTitle alone is replaced by the tab-title format as soon as the foreground process changes, so
        # the format itself is set for both the local (0) and remote (1) contexts.
        for context in ('0', '1'):
            call(session_path, 'setTabTitleFormat', context, title)

        # runCommand TYPES this text into the user's own interactive shell, which may not be sh (it may be
        # fish), so the inner command never travels as that text: it is written to a private sh script whose
        # first line deletes it and its directory, and the only thing typed is `exec sh '<path>'`.
        script_path = _write_posix_script(posix_inner_command(directory, cleared, forced, executable, arguments))
    except LaunchTimeout:
        # The session definitely exists (newSession already returned one) and no inner command was ever
        # typed into it, so this is a definite failure, not an open question -- unlike the generic
        # LaunchTimeout wording, never "may still have taken effect": closing the empty tab is attempted
        # right here, and the only thing actually left uncertain is whether that closing itself worked.
        closed = abandon()
        message = 'Konsole did not confirm the tab title within 30 seconds, so the launch failed.'
        if not closed:
            message += ' Closing the empty tab also failed; an empty Konsole tab may have been left open.'
        raise LaunchError(message) from None
    except BaseException:
        abandon()
        raise

    try:
        call(session_path, 'runCommand', f'exec sh {shlex.quote(str(script_path))}')
    except LaunchTimeout:
        # The command may already have been typed and be running; removing its script or typing `exit`
        # into the session now would break a launch that is actually under way.
        raise
    except BaseException:
        abandon()
        raise


def _write_posix_script(inner):
    temp_dir = tempfile.mkdtemp(prefix='agent-cli-tab-')
    script_path = Path(temp_dir) / 'launch.sh'
    script_path.write_text('rm -rf "$(dirname "$0")"\n' + shlex.join(inner) + '\n')
    script_path.chmod(0o700)
    return script_path


def _launch_windows_terminal(directory, executable, title, arguments, cleared, forced, environ, run, popen):
    terminal = shutil.which('wt.exe')
    if not terminal:
        raise LaunchError('wt.exe was not found on PATH.')

    # --window 0 is WT's documented sentinel for "the window that most recently had focus". This is not
    # the same as omitting the flag: windowingBehavior is unset by default, so no flag at all means
    # useNew -- always a separate OS window. Do not simplify this away, and do not swap it for
    # --window new, which forces the opposite of what it looks like it asks for.
    terminal_arguments = ['--window', '0', 'new-tab', '--startingDirectory', str(directory), '--title', title,
                           '--useApplicationTitle', executable, *arguments]
    escaped = [terminal_argument(value) for value in terminal_arguments]

    # The tab process inherits the environment of this wt.exe invocation, so the changes travel through
    # its own `env=` rather than by mutating this process's environment, which would leak them back into
    # the calling session.
    _client(run, [terminal, *escaped], 'Windows Terminal', env=_environment_for(environ, cleared, forced))


# Checked in this order because it is the order a launcher process could be nested in another: tmux
# inside kitty inside Konsole inside Windows Terminal sees TMUX first, so the innermost multiplexer the
# caller is actually attached to wins over an outer terminal emulator it happens to also be running in.
# Each handler runs only on the platform it can start a tab from: WT_SESSION also reaches WSL through
# WSLENV, and TMUX reaches Windows Python started from an MSYS or Cygwin tmux pane, and neither terminal
# can start the other platform's executable.
_TAB_HANDLERS = (
    ('TMUX', 'posix', _launch_tmux),
    ('KITTY_WINDOW_ID', 'posix', _launch_kitty),
    ('KONSOLE_DBUS_WINDOW', 'posix', _launch_konsole),
    ('WT_SESSION', 'windows', _launch_windows_terminal),
)


def _launch_new_window(directory, executable, title, arguments, cleared, forced, environ, run, popen):
    if IS_WINDOWS:
        if shutil.which('wt.exe'):
            # Still a tab: --window 0 opens it in the Windows Terminal window used most recently.
            return _launch_windows_terminal(directory, executable, title, arguments, cleared, forced, environ, run, popen)
        print('warning: no terminal was detected; opening a new window instead of a tab', file=sys.stderr)
        try:
            popen([executable, *arguments], cwd=str(directory), env=_environment_for(environ, cleared, forced),
                  creationflags=subprocess.CREATE_NEW_CONSOLE)
        except OSError as exc:
            raise LaunchError(f'Could not open a new console window: {exc}') from exc
        return

    # Without a display an X11 or Wayland terminal emulator starts and exits at once, which would read as
    # a launch. macOS sessions set neither variable and need no such check.
    if sys.platform != 'darwin' and not (environ.get('DISPLAY') or environ.get('WAYLAND_DISPLAY')):
        raise LaunchError('No terminal was detected and there is no graphical display to open a new window on.')
    print('warning: no terminal was detected; opening a new window instead of a tab', file=sys.stderr)

    inner = posix_inner_command(directory, cleared, forced, executable, arguments)
    candidates = []
    terminal_env = environ.get('TERMINAL')
    if terminal_env:
        candidates.append([terminal_env, '-e', *inner])
    candidates.append(['kitty', '--title', title, *inner])
    candidates.append(['konsole', '--workdir', str(directory), '-e', *inner])
    candidates.append(['xterm', '-e', *inner])

    for candidate in candidates:
        if not shutil.which(candidate[0]):
            continue
        try:
            popen(candidate, start_new_session=True,
                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            continue

    raise LaunchError('No terminal was detected and no terminal emulator was found on PATH to open a new window.')


def launch_tab(working_directory, executable, title, arguments=(), clear=(), force=None,
               environ=os.environ, run=subprocess.run, popen=subprocess.Popen):
    """Open <executable> <arguments> as a new tab in the terminal this process is already running inside.

    Detects the terminal from its own environment variables and uses its native new-tab command. Only
    with no terminal detected does this open a new window, with a warning -- a tab is the contract every
    caller relies on. A detected handler that fails raises rather than falling back to a window: a silent
    new window is a worse surprise than a loud failure.
    """
    # Made absolute here, because a relative path would be re-resolved against the terminal's own cwd in
    # the tab.
    directory = resolve_tab_directory(working_directory)

    clear = list(clear)
    force = dict(force or {})
    if not IS_WINDOWS:
        # The terminal that starts the tab sets TERM for that session itself; forcing or clearing it here
        # would fight that.
        clear = [name for name in clear if name != 'TERM']
        force.pop('TERM', None)

    cleared, forced = launch_environment(clear=clear, force=force)
    # The tab title is the only name for this session the user can see on screen, so the session records
    # it at SessionStart and peer-cli resolves by it.
    forced = dict(forced)
    forced['AGENT_CLI_TAB_TITLE'] = title
    arguments = list(arguments)

    platform = 'windows' if IS_WINDOWS else 'posix'
    for name, handler_platform, handler in _TAB_HANDLERS:
        if handler_platform == platform and environ.get(name):
            return handler(directory, executable, title, arguments, cleared, forced, environ, run, popen)

    return _launch_new_window(directory, executable, title, arguments, cleared, forced, environ, run, popen)


def startup_binding_script():
    path = HERE / "startup_binding.py"
    if not path.is_file():
        raise LaunchError(f"startup binding script was not found beside {HERE}")
    return path


def handoff_status_path():
    return Path(tempfile.mkdtemp(prefix="tj-agents-handoff-")) / "status.json"


def await_handoff_startup(status_path, timeout=480, wrapper_timeout=45,
                          clock=time.monotonic, wait=time.sleep):
    spec = importlib.util.spec_from_file_location("handoff_launch_status", HERE / "launch_status.py")
    status_file = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(status_file)
    start = clock()
    while True:
        status = status_file.read(status_path) or {}
        state = status.get("state")
        if state == "abandoned":
            raise LaunchError("the launch was abandoned before startup")
        if state == "failed":
            raise LaunchError(str(status.get("error") or "native startup failed"))
        if state == "unconfirmed":
            raise LaunchTimeout(f"native CLI is still running, but SessionStart was not acknowledged; "
                                f"leave the tab open and do not relaunch; receipt: {status_path}")
        if state == "running" or (state == "exited" and status.get("running_at") and status.get("returncode") == 0):
            return
        if state == "exited":
            raise LaunchError(f"native CLI exited with {status.get('returncode')} before handoff confirmation")
        elapsed = clock() - start
        if not status and elapsed >= wrapper_timeout:
            try:
                abandoned = status_file.create(status_path, {"state": "abandoned", "at": time.time()})
            except OSError as error:
                raise LaunchTimeout(f"launch status could not be recorded: {error}; receipt: {status_path}") from error
            if abandoned:
                raise LaunchError("the terminal never started the startup wrapper; the late launch is blocked")
        if elapsed >= timeout:
            raise LaunchTimeout(f"native SessionStart was not confirmed; receipt: {status_path}")
        wait(0.1)


def bound_terminal_arguments(host, working_directory, target, arguments, status_file=None, require_session_start=False):
    status = ["--status-file", str(status_file)] if status_file else []
    return ["-I", "-B", str(startup_binding_script()), "--host", host, "--project",
            str(working_directory), "--target", str(target), *status,
            *(["--require-session-start"] if require_session_start else []), "--", *arguments]
