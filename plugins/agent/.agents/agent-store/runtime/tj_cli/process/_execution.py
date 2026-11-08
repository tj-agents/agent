import locale
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import IO, Any, TypedDict, Unpack

type _Command = Sequence[str | os.PathLike[str]]
type _Stream = int | IO[str] | IO[bytes] | None


class _StartOptions(TypedDict, total=False):
    stdin: _Stream
    stdout: _Stream
    stderr: _Stream
    cwd: str | os.PathLike[str] | None
    env: Mapping[str, str] | None
    text: bool
    encoding: str | None
    errors: str | None
    bufsize: int
    close_fds: bool
    creationflags: int
    start_new_session: bool
    shell: bool
    executable: str | os.PathLike[str] | None


class _RunOptions(_StartOptions, total=False):
    input: str | bytes | None


def start(command: _Command, **options: Unpack[_StartOptions]) -> subprocess.Popen[Any]:
    return _start(command, options)


def _start(command: _Command, options: _StartOptions) -> subprocess.Popen[Any]:
    if unknown := options.keys() - _StartOptions.__optional_keys__:
        raise TypeError(f"unsupported process options: {', '.join(sorted(unknown))}")
    if not command:
        raise ValueError("an executable is required")
    if options.pop("shell", False):
        raise ValueError("hidden processes require an explicit executable")
    if "executable" in options:
        raise ValueError("the executable belongs in the command")
    launcher = _launcher()
    if launcher is None:
        raise FileNotFoundError("tj_hook is not installed on PATH")
    if sys.platform == "win32":
        streams = (options.get("stdin"), options.get("stdout"), options.get("stderr"))
        if all(stream is None for stream in streams):
            try:
                os.fstat(0)
            except OSError:
                options["stdin"] = subprocess.DEVNULL
            else:
                options["stdin"] = 0
        flags = options.pop("creationflags", 0)
        if flags & (subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS):
            raise ValueError(
                "hidden processes cannot request a visible or detached console"
            )
        options["creationflags"] = flags | subprocess.CREATE_NO_WINDOW
    return subprocess.Popen([launcher, "--", *command], **options)


def _launcher() -> str | None:
    launcher = None
    if sys.platform == "win32":
        for directory in os.get_exec_path():
            candidate = Path(directory).absolute() / "tj_hook.exe"
            if shutil.which(candidate) is not None:
                launcher = str(candidate)
                break
    else:
        launcher = shutil.which("tj_hook")
    return str(Path(launcher).absolute()) if launcher is not None else None


def run(
    command: _Command,
    *,
    capture_output: bool = False,
    timeout: float | None = None,
    check: bool = False,
    **options: Unpack[_RunOptions],
) -> subprocess.CompletedProcess[Any]:
    input_data = options.pop("input", None)
    if input_data is not None:
        if "stdin" in options:
            raise ValueError("stdin and input arguments may not both be used")
        options["stdin"] = subprocess.PIPE
    if capture_output:
        if "stdout" in options or "stderr" in options:
            raise ValueError("stdout and stderr cannot be used with capture_output")
        options["stdout"] = options["stderr"] = subprocess.PIPE
    if sys.platform != "win32":
        options["start_new_session"] = True
    with _start(command, options) as process:
        try:
            stdout, stderr = process.communicate(input_data, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            _kill_tree(process)
            error.cmd = command
            if sys.platform == "win32":
                stdout, stderr = process.communicate()
                encoding = options.get("encoding") or locale.getpreferredencoding(False)
                errors = options.get("errors") or "strict"
                error.output = _timeout_output(stdout, encoding, errors)
                error.stderr = _timeout_output(stderr, encoding, errors)
            else:
                process.wait()
            raise
        except BaseException:
            _kill_tree(process)
            process.wait()
            raise
        result = subprocess.CompletedProcess(
            command, process.returncode, stdout, stderr
        )
        if check:
            result.check_returncode()
        return result


def _kill_tree(process: subprocess.Popen[Any]) -> None:
    if sys.platform == "win32":
        process.kill()
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def _timeout_output(value: object, encoding: str, errors: str) -> bytes | None:
    if isinstance(value, str):
        return value.encode(encoding, errors)
    if isinstance(value, bytes):
        return value
    return None
