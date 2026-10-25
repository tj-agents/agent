#!/usr/bin/env python3
"""Launch a native CLI and report handoff acknowledgement (Python 3.9+)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import launch_check
import launch_status
import required_plugins
from terminal_command import command_for_target, without_binding_state

ACK_DIRECTORY_ENV = "TJ_AGENTS_STARTUP_ACK_DIRECTORY"
ACK_TOKEN_ENV = "TJ_AGENTS_STARTUP_ACK_TOKEN"
STARTUP_TIMEOUT_SECONDS = 20


class StartupBindingError(RuntimeError):
    pass


def _acknowledged(directory: Path, token: str) -> bool:
    try:
        return (directory / token).read_text(encoding="utf-8") == token
    except OSError:
        return False


def _terminate(process: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def run_session(host: str, target: str, project: Path, arguments: Sequence[str], *,
                extra_environment: Mapping[str, str] | None = None,
                timeout: float = STARTUP_TIMEOUT_SECONDS,
                on_running: Callable[..., None] | None = None,
                on_unconfirmed: Callable[..., None] | None = None,
                require_session_start: bool = False,
                on_phase: Callable[[str], None] | None = None) -> int:
    phase = on_phase or (lambda _name: None)
    running = on_running or (lambda child_pid=None: None)
    environment = without_binding_state(os.environ)
    if extra_environment:
        environment.update(without_binding_state(extra_environment))
    with tempfile.TemporaryDirectory(prefix="tj-agents-launch-") as temporary:
        directory = Path(temporary)
        token = secrets.token_urlsafe(32)
        if require_session_start:
            environment[ACK_DIRECTORY_ENV] = str(directory)
            environment[ACK_TOKEN_ENV] = token
        phase("starting-native")
        try:
            process = subprocess.Popen(command_for_target(target, list(arguments)),
                                       cwd=project, env=environment, start_new_session=os.name != "nt")
        except OSError as error:
            raise StartupBindingError(f"native {host} could not start: {error}") from error
        try:
            if require_session_start:
                phase("waiting-session-start")
                deadline = time.monotonic() + timeout
                unconfirmed = False
                while not _acknowledged(directory, token):
                    if process.poll() is not None:
                        return process.returncode
                    if not unconfirmed and time.monotonic() >= deadline:
                        if on_unconfirmed is not None:
                            on_unconfirmed(process.pid)
                        print(f"launch: native {host} is still running, but SessionStart was not acknowledged; "
                              "leaving the session open; do not relaunch", file=sys.stderr, flush=True)
                        unconfirmed = True
                    time.sleep(0.05)
            running(process.pid)
            return process.wait()
        except BaseException:
            if process.poll() is None:
                try:
                    _terminate(process)
                except (OSError, subprocess.SubprocessError) as error:
                    print(f"launch: native child cleanup failed: {error}", file=sys.stderr)
            raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("claude", "codex"), required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--require-session-start", action="store_true")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(argv)
    arguments = parsed.arguments[1:] if parsed.arguments[:1] == ["--"] else parsed.arguments
    status: dict[str, object] = {"pid": os.getpid()}
    if parsed.host == "claude" and parsed.require_session_start and parsed.status_file is not None:
        if not any(value == "--debug-file" or value.startswith("--debug-file=") for value in arguments):
            debug_log = parsed.status_file.resolve().parent / "native-debug.log"
            arguments = ["--debug-file", str(debug_log), *arguments]
            status["debug_log"] = str(debug_log)

    def report(state: str, **details: object) -> None:
        status.update(details, state=state, at=time.time())
        if parsed.status_file is not None:
            try:
                launch_status.replace(parsed.status_file, status)
            except OSError:
                pass

    if parsed.status_file is not None:
        try:
            claimed = launch_status.create(parsed.status_file, {**status, "state": "preparing", "at": time.time()})
        except OSError as error:
            print(f"launch: {parsed.host} launch status could not be recorded: {error}", file=sys.stderr)
            return 1
        if not claimed:
            print(f"launch: {parsed.host} launch was abandoned or already claimed before startup", file=sys.stderr)
            return 1

    def phase(name: str) -> None:
        if parsed.status_file is not None:
            report("preparing", phase=name)
            print(f"launch: {parsed.host} startup: {name}", flush=True)

    phase("checking-plugins")
    try:
        effective_project = required_plugins.check_launch(parsed.project.resolve(), parsed.host, parsed.target, arguments)
    except required_plugins.RequiredPluginsError as error:
        message = f"launch: {error}"
        print(message, file=sys.stderr)
        report("failed", error=message)
        return 1
    try:
        result = launch_check.check(effective_project or parsed.project.resolve(), host=parsed.host)
    except Exception as error:
        print(f"plugins: update check failed: {error}", file=sys.stderr)
        result = "failed"

    if effective_project is not None:
        try:
            launch_check.require_update_check(result)
        except RuntimeError as error:
            message = f"launch: {error}"
            print(message, file=sys.stderr)
            report("failed", error=message)
            return 1

    if parsed.host == "codex" and effective_project is not None:
        phase("verifying-required-hooks")
        try:
            launch_check.ensure_hooks(effective_project, parsed.target)
        except Exception as error:
            message = f"launch: required hook readiness failed: {error}"
            print(message, file=sys.stderr)
            report("failed", error=message)
            return 1

    try:
        options = {"require_session_start": True} if parsed.require_session_start else {}
        if parsed.status_file is not None:
            options["on_phase"] = phase
        returncode = run_session(parsed.host, parsed.target, parsed.project, arguments, **options,
                                 on_unconfirmed=lambda child_pid: report("unconfirmed", child_pid=child_pid),
                                 on_running=lambda child_pid=None: report("running", running_at=time.time(), child_pid=child_pid))
    except StartupBindingError as error:
        message = f"launch: {parsed.host} launch failed: {error}"
        print(message, file=sys.stderr)
        report("failed", error=message)
        return 1
    report("exited", returncode=returncode)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
