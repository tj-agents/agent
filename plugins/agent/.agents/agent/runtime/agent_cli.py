"""Shared launch primitives for the native agent CLIs, on Windows and POSIX.

Imported by Agent's private handoff launchers and session recovery so the environment scrub, executable
discovery, lane lookup and terminal argument escaping have one owner. Not runnable on its own.
"""

import glob
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

HERE = Path(__file__).resolve().parent
IS_WINDOWS = os.name == 'nt'

# Session state a Claude Code parent exports into anything it spawns. NO_COLOR=1 alone makes the child
# black and white; the CLAUDE_CODE_* set binds it to the parent's messaging pipe and makes it behave as a
# managed nested child rather than the independent session the caller asked for. CLAUDE_CODE_GIT_BASH_PATH
# is machine configuration rather than session state and is deliberately absent from this list.
SESSION_ENVIRONMENT = (
    'NO_COLOR',
    'CLAUDECODE',
    'CLAUDE_CODE_CHILD_SESSION',
    'CLAUDE_CODE_ENTRYPOINT',
    'CLAUDE_CODE_SESSION_ID',
    'CLAUDE_CODE_MESSAGING_SOCKET',
    'CLAUDE_CODE_MESSAGING_TOKEN',
    'CLAUDE_PID',
    'WORKBOARD_LAUNCH_TOKEN',
    'WORKBOARD_WORKFLOW_TOKEN',
)


class LaunchError(Exception):
    """A launch precondition failed; the message is the whole report."""


class LaunchTimeout(LaunchError):
    """A terminal control command did not answer in time, so whether it took effect is unknown."""


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
    cleared = list(dict.fromkeys((*SESSION_ENVIRONMENT, *clear)))
    forced = dict(force or {})
    return [name for name in cleared if name not in forced], forced


def _is_script(path):
    try:
        with open(path, 'rb') as f:
            return f.read(2) == b'#!'
    except OSError:
        return True


# The native executable, never the npm shim: a shim launches the TUI through an extra node process and
# degrades it to monochrome and non-interactive, the failure handoff-codex first recorded. On Windows
# `claude` on PATH resolves to that shim (claude.ps1/claude.cmd), so PATH is consulted only for a real
# claude.exe; on POSIX the shim is a `#!` script, and npm with its prefix at ~/.local puts one at the native
# install's own path, so any candidate that is one is refused.
def resolve_claude_executable(home=None, which=shutil.which):
    home = Path(home) if home else Path.home()
    name = 'claude.exe' if IS_WINDOWS else 'claude'
    local = home / '.local' / 'bin' / name
    found = which(name)
    for candidate in (str(local), found):
        if candidate and os.path.isfile(candidate) and (IS_WINDOWS or not _is_script(os.path.realpath(candidate))):
            return candidate

    raise LaunchError(f'The native Claude Code executable was not found. Looked for {local} and a native {name} on PATH.')


_CODEX_VERSION = re.compile(r'(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?')


def parse_codex_version(text):
    match = _CODEX_VERSION.search(text or '')
    if not match:
        return None
    return {
        'core': tuple(int(match.group(i)) for i in (1, 2, 3)),
        'prerelease': match.group(4) or '',
        'text': match.group(0),
    }


def compare_codex_version(left, right):
    if left['core'] != right['core']:
        return -1 if left['core'] < right['core'] else 1
    # A prerelease ranks BELOW the release it precedes, so 0.151.0-alpha.7.1 loses to 0.151.0. Ranking by
    # file date instead is what silently selected a three-versions-stale alpha over the current CLI.
    if bool(left['prerelease']) != bool(right['prerelease']):
        return -1 if left['prerelease'] else 1
    if not left['prerelease']:
        return 0

    left_parts, right_parts = left['prerelease'].split('.'), right['prerelease'].split('.')
    for i in range(max(len(left_parts), len(right_parts))):
        if i >= len(left_parts):
            return -1
        if i >= len(right_parts):
            return 1
        a, b = left_parts[i], right_parts[i]
        if a.isdigit() and b.isdigit():
            a, b = int(a), int(b)
        if a != b:
            return -1 if a < b else 1
    return 0


def npm_global_prefix():
    """The npm global install prefix, or None when npm is unavailable or reports nothing."""
    try:
        result = subprocess.run(['npm', 'config', 'get', 'prefix'], capture_output=True, text=True,
                                 encoding='utf-8', errors='replace', timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    prefix = result.stdout.strip()
    return prefix or None


def codex_candidate_paths(which=shutil.which, local_app_data=None, npm_prefix=npm_global_prefix):
    name = 'codex.exe' if IS_WINDOWS else 'codex'
    candidates = []

    # The npm package is a JavaScript entry point over a vendored native binary per platform, and the entry
    # point is what lands on PATH. Launching that shim is the path that degrades the TUI to monochrome and
    # non-interactive, so the vendored executable underneath it is launched instead - identical bits,
    # without the node process in front. Windows npm puts the shim beside node_modules; POSIX npm symlinks
    # bin/codex to the package's own bin/codex.js, two levels below the package root.
    shim = which('codex')

    # A session launched from a desktop entry (not a login shell) often has PATH built from the display
    # manager rather than the shell rc files that add a user npm prefix's bin directory, so `which` finds
    # nothing even though the global install is on disk. `npm config get prefix` resolves it directly,
    # the same way the native install under home is checked for Claude, below any PATH lookup.
    if not shim and not IS_WINDOWS:
        prefix = npm_prefix()
        if prefix:
            direct = Path(prefix) / 'bin' / name
            if direct.is_file():
                shim = str(direct)

    # `codex` on PATH is not always that shim: a standalone install, or a PATH that puts the vendored
    # binary ahead of the npm one, lands the native executable on PATH directly. On POSIX that is whatever
    # is not a `#!` script; on Windows the npm entry point is a `.cmd`/`.ps1`, never a `.exe`, so any `.exe`
    # found here is native. Its realpath is included too, in case the PATH entry is itself a symlink.
    if shim and ((IS_WINDOWS and Path(shim).suffix.casefold() == '.exe')
                 or (not IS_WINDOWS and not _is_script(os.path.realpath(shim)))):
        candidates.append(shim)
        candidates.append(os.path.realpath(shim))

    if shim:
        roots = [
            Path(shim).parent / 'node_modules' / '@openai' / 'codex',
            Path(os.path.realpath(shim)).parent.parent,
        ]
        for root in dict.fromkeys(roots):
            pattern = str(root / 'node_modules' / '@openai' / 'codex-*' / 'vendor' / '*' / 'bin' / name)
            candidates.extend(sorted(glob.glob(pattern)))

    # The Windows desktop app caches each downloaded runtime in its own hash-named directory and adds rather
    # than replaces, so this folder accumulates stale payloads instead of holding only the current build.
    # There is no desktop app cache anywhere else.
    if IS_WINDOWS:
        local_app_data = local_app_data or os.environ.get('LOCALAPPDATA')
        if local_app_data:
            desktop = Path(local_app_data) / 'OpenAI' / 'Codex' / 'bin'
            candidates.extend(sorted(str(p) for p in desktop.rglob('codex.exe') if p.is_file()))

    return list(dict.fromkeys(candidates))


def resolve_codex_executable(minimum='0.154.0', candidates=None):
    """The newest native Codex executable that answers --version, as (path, version)."""
    paths = codex_candidate_paths() if candidates is None else candidates
    found = []
    for path in paths:
        try:
            result = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        version = parse_codex_version(result.stdout + result.stderr)
        if version:
            found.append((path, version))

    if not found:
        raise LaunchError(
            'No native Codex executable answered --version. Looked beside the `codex` shim on PATH for the npm '
            "package's vendored binary" + (', and under the desktop app cache.' if IS_WINDOWS else '.')
        )

    best = found[0]
    for candidate in found[1:]:
        if compare_codex_version(candidate[1], best[1]) > 0:
            best = candidate

    floor = parse_codex_version(minimum)
    if floor is None:
        raise LaunchError(f'The minimum Codex version is not a version: {minimum}')
    if compare_codex_version(best[1], floor) < 0:
        inventory = '\n'.join(f'  {version["text"]}\t{path}' for path, version in found)
        raise LaunchError(
            f'The newest native Codex executable found is {best[1]["text"]}, below the required {minimum}.\n'
            'The model roster is gated on the CLI version, so launching this build would silently drop newer models.\n'
            'Update with: npm install -g @openai/codex@latest\n'
            f'Found:\n{inventory}'
        )
    return best


def claude_config_dir():
    configured = os.environ.get('CLAUDE_CONFIG_DIR')
    return Path(configured) if configured else Path.home() / '.claude'


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

    Not .resolve(): on Windows that rewrites a mapped or subst drive to its target, and Claude Code keys a
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
    _client(run, ['tmux', 'new-window', '-n', title, '--', *inner], 'tmux new-window')


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
                  '--tab-title', title, *inner], 'kitty @ launch')


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


def open_claude_tab(working_directory, title, arguments, out=print, status_file=None, require_session_start=False,
                    clear=(), force=None):
    claude = resolve_claude_executable()

    # Forced, not merely un-cleared: an automation-spawned terminal tab is not the interactive shell a
    # human would have launched it from, so colour/terminal-capability auto-detection cannot be trusted to
    # land on a good value on its own. FORCE_COLOR is the de-facto Node CLI convention (chalk/supports-color)
    # to force colour outright. TERM=xterm-256color is forced the same way, but only ever reaches the
    # session on Windows: launch_tab drops any forced TERM on POSIX, because the terminal that actually
    # starts the tab sets TERM for that session itself, and forcing or clearing it here would fight that.
    launch_tab(working_directory, sys.executable, title,
               arguments=bound_terminal_arguments("claude", working_directory, claude, list(arguments), status_file, require_session_start),
               clear=clear, force={'FORCE_COLOR': '1', 'TERM': 'xterm-256color', **(force or {})})
