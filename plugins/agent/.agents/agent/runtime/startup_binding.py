#!/usr/bin/env python3
"""Launch a native CLI and report handoff acknowledgement (Python 3.9+)."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
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


def _finite_identity(info):
    started = getattr(info, "started_at", None)
    if (info is None or not isinstance(getattr(info, "pid", None), int) or isinstance(info.pid, bool)
            or not isinstance(started, (int, float)) or isinstance(started, bool) or not math.isfinite(started)):
        return None
    return {"pid": info.pid, "started_at": started}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _windows_tab_binding(terminal: dict) -> dict | None:
    """Capture a unique UIA tab id at launch; a title is never later used to close it."""
    if os.name != "nt" or terminal.get("kind") != "windows-terminal":
        return None
    title = os.environ.get(register_session.TITLE_ENV)
    if not isinstance(title, str) or not title:
        return None
    for root in Path(__file__).resolve().parents:
        if not (root / ".agents" / "agent" / "runtime").is_dir():
            continue
        path = root / ".agents" / "agent" / "utility" / "peer" / "scripts" / "close_tab.py"
        if not path.is_file():
            return None
        try:
            spec = importlib.util.spec_from_file_location("agent_startup_close_tab", path)
            close_tab = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(close_tab)
            matches = [item for item in close_tab.windows_inventory() or [] if item.get("title") == title]
            if len(matches) != 1:
                return None
            automation_id = matches[0].get("identity", {}).get("automation_id")
            if not isinstance(automation_id, str) or not automation_id:
                return None
            return {"automation_id": automation_id, "observed_title": title,
                    "evidence": "windows-uia-unique-title-at-launch"}
        except Exception:
            return None
    return None


def _record_launch_binding(host: str, child_pid: int) -> None:
    """Bind an acknowledged SessionStart to this wrapper's exact Popen child."""
    try:
        lookup = register_session.build_process_lookup()
        wrapper, child = lookup(os.getpid()), lookup(child_pid)
        wrapper_identity, child_identity = _finite_identity(wrapper), _finite_identity(child)
        if not wrapper_identity or not child_identity or child.ppid != wrapper.pid:
            return
        terminal = register_session.terminal_identity()
        if not isinstance(terminal, dict) or not terminal.get("kind"):
            return
        terminal = {**terminal, "binding": wrapper_identity}
        tab_binding = _windows_tab_binding(terminal)
        if tab_binding is not None:
            terminal["tab_binding"] = tab_binding
        matches = []
        for path in register_session.state_directory().glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (isinstance(record, dict) and record.get("host") == host and record.get("pid") == child_pid
                    and record.get("pid_started_at") == child_identity["started_at"]
                    and isinstance(record.get("session_id"), str) and isinstance(record.get("profile"), str)):
                matches.append((path, record))
        if len(matches) != 1:
            return
        path, record = matches[0]
        namespace = hashlib.sha256(record["profile"].encode("utf-8")).hexdigest()[:16]
        receipt = register_session.state_directory().parent / "recovery-launches" / (
            f"{host}-{namespace}-{record['session_id']}.json")
        _write_json(receipt, {"host": host, "session": record["session_id"], "profile": record["profile"],
                              "state": "launched", "created_at": time.time(),
                              "wrapper": {**wrapper_identity, "child": child_identity, "terminal": terminal,
                                          "title": os.environ.get(register_session.TITLE_ENV)}})
        record.update({"frontend": child_identity, "terminal": terminal,
                       "provenance": {"source": "agent-startup-binding", "receipt": str(receipt)}})
        _write_json(path, record)
    except (OSError, ValueError, OverflowError):
        return


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
                    _record_launch_binding(host, process.pid)
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
