import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from typing import IO, Any, NamedTuple, Protocol

from tj_cli.clipboard._errors import ClipboardError
from tj_cli.clipboard._guard import refuse_real_backend_in_tests
from tj_cli.clipboard._text import strip_trailing_whitespace


class ProcessResult(Protocol):
    """What `_run_clipboard_process` actually reads off a completed process -- not the
    full `subprocess.CompletedProcess` surface, so a test's fake result only needs
    these three fields.

    Declared as read-only properties, not plain attributes, so a frozen dataclass fake
    matches: a Protocol's plain attribute declarations require a settable attribute,
    which `CompletedProcess` itself has but a frozen fake does not.
    """

    @property
    def returncode(self) -> int: ...
    @property
    def stdout(self) -> str: ...
    @property
    def stderr(self) -> str: ...


type RunFunction = Callable[..., ProcessResult]
type WhichFunction = Callable[[str], str | None]


class PosixTool(NamedTuple):
    label: str
    copy_argv: list[str]
    paste_argv: list[str]
    env_overrides: dict[str, str]


_WL_COPY = PosixTool("wl-copy", ["wl-copy"], ["wl-paste", "--no-newline"], {})
_XCLIP = PosixTool(
    "xclip",
    ["xclip", "-selection", "clipboard"],
    ["xclip", "-selection", "clipboard", "-o"],
    {},
)
_XSEL = PosixTool(
    "xsel", ["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--output"], {}
)
_PBCOPY = PosixTool(
    "pbcopy",
    ["pbcopy"],
    ["pbpaste"],
    # pbcopy/pbpaste pick their text encoding from the locale; forcing a UTF-8 one here
    # is what keeps non-ASCII draft text (accents, emoji) intact rather than silently
    # mangled on some machines.
    {"LANG": "en_US.UTF-8", "LC_CTYPE": "en_US.UTF-8"},
)


def posix_clipboard_tool(environ: Mapping[str, str], which: WhichFunction) -> PosixTool:
    """The PosixTool this environment selects, or a ClipboardError naming what to
    install.

    Wayland is checked first because a Wayland session under XWayland can still have
    DISPLAY set; without wl-copy/wl-paste on PATH, such a session falls back to the X11
    tools rather than failing outright. Either way, nothing usable in a Wayland session
    is one error, naming wl-clipboard first (the native tool for the session actually
    running), and xclip/xsel only when DISPLAY is set, the one case they could be used.
    """
    wayland = bool(environ.get("WAYLAND_DISPLAY"))
    x11 = bool(environ.get("DISPLAY"))

    if wayland:
        if which("wl-copy") and which("wl-paste"):
            return _WL_COPY
        if x11:
            if which("xclip"):
                return _XCLIP
            if which("xsel"):
                return _XSEL
        fallback = ", or xclip/xsel as an XWayland fallback" if x11 else ""
        raise ClipboardError(
            "No clipboard tool was found on PATH. Install wl-clipboard (provides "
            f"wl-copy/wl-paste){fallback}."
        )

    if x11:
        if which("xclip"):
            return _XCLIP
        if which("xsel"):
            return _XSEL
        raise ClipboardError(
            "No X11 clipboard tool was found on PATH. Install xclip or xsel."
        )

    if sys.platform == "darwin":
        if which("pbcopy") and which("pbpaste"):
            return _PBCOPY
        raise ClipboardError("pbcopy/pbpaste was not found on PATH.")

    raise ClipboardError(
        "No supported clipboard session was detected: neither WAYLAND_DISPLAY nor "
        "DISPLAY is set, and this is not macOS."
    )


def _clipboard_failure(
    label: str, returncode: int, stderr: str | None
) -> ClipboardError:
    detail = (stderr or "").strip()
    message = f"{label} failed (exit {returncode})"
    return ClipboardError(f"{message}: {detail}" if detail else f"{message}.")


_TRANSIENT_READBACK_PATTERNS = (
    "nothing is copied",
    "no suitable type",
    "target not available",
    "no selection",
)


def _is_transient_readback_failure(stderr: str | None) -> bool:
    """Whether a nonzero read-back exit names a known "owner not ready yet" shape worth
    retrying.

    wl-paste says "Nothing is copied" when no owner has claimed the selection yet; xclip
    says "No suitable type" or "target not available" for the same instant; xsel says
    "no selection". Any other nonzero exit is treated as a real failure -- retrying an
    error this has no reason to believe is transient would just spend the whole retry
    budget repeating it before reporting the same thing anyway.
    """
    lowered = (stderr or "").lower()
    return any(pattern in lowered for pattern in _TRANSIENT_READBACK_PATTERNS)


def _run_clipboard_process(
    run: RunFunction,
    argv: list[str],
    label: str,
    env: Mapping[str, str] | None = None,
    timeout: float = 10,
    input_text: str | None = None,
    capture_stdout: bool = False,
    may_fork: bool = False,
) -> tuple[int, str, str]:
    """Runs one clipboard tool invocation; returns (returncode, stdout_text,
    stderr_text).

    Never raises for a nonzero exit -- that is left to the caller, since a read-back's
    nonzero exit can be routine while a just-started owner is still taking over (see
    `_is_transient_readback_failure`), not necessarily a real failure. Only a process
    that could not even be started or answer within the timeout becomes a ClipboardError
    here.

    stdout is discarded (DEVNULL) unless `capture_stdout` -- a copy command's stdout is
    never read.

    stderr is captured differently depending on `may_fork`. A copy command
    (`may_fork=True`) -- wl-copy, xclip, xsel -- forks a long-lived background process
    that goes on serving the selection after this call returns, and that fork inherits
    this call's stdio; a PIPE would stay open (and communicate() keep waiting on it)
    until that fork also exits, so a copy that actually succeeded immediately would hang
    until the timeout. A private temporary file has no such problem, so the failing
    tool's own stderr can still be reported. A read-back command never forks, so its
    stderr is captured the ordinary way, via PIPE, alongside its stdout.

    Both streams are decoded explicitly as UTF-8 with decoding errors replaced, rather
    than left to the process's locale -- the locale is exactly what forcing
    LANG/LC_CTYPE for pbcopy/pbpaste routes around.
    """
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if input_text is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = input_text
    kwargs["stdout"] = subprocess.PIPE if capture_stdout else subprocess.DEVNULL
    if env is not None:
        kwargs["env"] = env

    # Not a `with` block: the file must outlive this statement, serving as `stderr=` for
    # a possibly-forked subprocess, and is closed explicitly in the `finally` below.
    stderr_file: IO[str] | None = (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")  # noqa: SIM115
        if may_fork
        else None
    )
    try:
        kwargs["stderr"] = stderr_file if stderr_file is not None else subprocess.PIPE
        try:
            result = run(argv, **kwargs)
        except subprocess.TimeoutExpired:
            raise ClipboardError(
                f"{label} did not answer within {timeout} seconds."
            ) from None
        except OSError as exc:
            raise ClipboardError(f"{label} could not be run: {exc}") from None
        if stderr_file is not None:
            stderr_file.seek(0)
            stderr_text = stderr_file.read()
        else:
            stderr_text = result.stderr or ""
    finally:
        if stderr_file is not None:
            stderr_file.close()

    stdout_text = result.stdout if capture_stdout else ""
    return result.returncode, (stdout_text or ""), stderr_text


def _run_copy(
    run: RunFunction,
    argv: list[str],
    text: str,
    label: str,
    env: Mapping[str, str] | None = None,
    timeout: float = 10,
) -> None:
    returncode, _, stderr = _run_clipboard_process(
        run,
        argv,
        label,
        env=env,
        timeout=timeout,
        input_text=text,
        capture_stdout=False,
        may_fork=True,
    )
    if returncode != 0:
        raise _clipboard_failure(label, returncode, stderr)


def _read_back_with_retry(
    run: RunFunction,
    argv: list[str],
    label: str,
    is_ok: Callable[[str], bool],
    env: Mapping[str, str] | None = None,
    attempts: int = 5,
    total_timeout: float = 1.0,
) -> str:
    """Reads back up to `attempts` times, pausing between tries, until `is_ok` accepts
    the output.

    A nonzero exit is retried only when `_is_transient_readback_failure` recognises its
    stderr as the owner-not-ready-yet shape; any other nonzero exit fails immediately
    with that attempt's own stderr. If every retried attempt keeps exiting nonzero
    (transiently), the last one's tool failure is raised, carrying its stderr. If at
    least one attempt exits zero but its content is never accepted by `is_ok`, the last
    such zero-exit output is returned instead, so the caller raises its own content-
    mismatch message -- a read-back that worked but did not match is a different failure
    from the tool never answering at all.
    """
    delay = total_timeout / attempts
    returncode, stderr = 1, ""
    last_zero_exit_output: str | None = None
    for attempt in range(attempts):
        returncode, output, stderr = _run_clipboard_process(
            run, argv, label, env=env, capture_stdout=True
        )
        if returncode == 0:
            if is_ok(output):
                return output
            last_zero_exit_output = output
        elif not _is_transient_readback_failure(stderr):
            raise _clipboard_failure(label, returncode, stderr)
        if attempt < attempts - 1:
            time.sleep(delay)
    if last_zero_exit_output is None:
        raise _clipboard_failure(label, returncode, stderr)
    return last_zero_exit_output


def copy_posix(
    text: str,
    environ: Mapping[str, str] | None = None,
    which: WhichFunction = shutil.which,
    run: RunFunction = subprocess.run,
) -> str:
    """Copy `text` through the clipboard tool this environment selects, and verify it
    round-trips.
    """
    refuse_real_backend_in_tests(run is subprocess.run)
    environ = os.environ if environ is None else environ
    tool = posix_clipboard_tool(environ, which)
    env = {**environ, **tool.env_overrides} if tool.env_overrides else None
    _run_copy(run, tool.copy_argv, text, f"{tool.label} copy", env=env)

    def plain_ok(output: str) -> bool:
        return strip_trailing_whitespace(output) == text

    landed = _read_back_with_retry(
        run, tool.paste_argv, f"{tool.label} paste", plain_ok, env=env
    )
    if not plain_ok(landed):
        raise ClipboardError(
            f"Clipboard verification failed: {tool.label} did not read back what was "
            "copied."
        )
    return f"Copied {len(text)} chars."
