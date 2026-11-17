from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import agent_store
import transcripts
from codex import daemon, hook_trust, permissions, projects
from launch_contract import PreparedLaunch
from launch_errors import LaunchError, LaunchTimeout, cleanup_failed_launch
from package_root import find_package_root
from tj_cli import process

MINIMUM_VERSION = "0.167.1"
IS_WINDOWS = os.name == "nt"
# Codex refuses to start its daemon in a job that would end it with the caller,
# such as an agent's tool shell; a Windows Terminal tab is outside that job.
JOB_REFUSAL = "prevents background processes from outliving it"
DAEMON_WAIT_SECONDS = 30
DAEMON_IDENTITY = frozenset(
    {
        "AGENT_CLI_TAB_TITLE",
        "CLAUDECODE",
        "CLAUDE_PID",
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CODEX_TOOL_CALL_ID",
        "WT_SESSION",
        "TMUX_PANE",
    }
)


def daemon_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in environ.items()
        if name.upper() not in DAEMON_IDENTITY
        and not name.upper().startswith(("TJ_RUN_", "CLAUDE_CODE_"))
    }


class Server(Protocol):
    def request(self, method: str, params: dict[str, object]) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class StartOptions:
    model: str | None
    effort: str | None
    prompt: str


def inside_wsl() -> bool:
    # platform.release() queries WMI on Windows, which can hang.
    if sys.platform == "win32":
        inside = False
    else:
        inside = "microsoft" in os.uname().release.lower()
    return inside


def native_id(value: object) -> str:
    if isinstance(value, str):
        try:
            parsed = UUID(value)
        except ValueError as error:
            raise LaunchError(
                "Codex did not supply a canonical native thread UUID"
            ) from error
        if str(parsed) == value:
            return value
    raise LaunchError("Codex did not supply a canonical native thread UUID")


def require_binding(cli: str) -> None:
    result = process.run(
        [cli, "_session", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode or not any(
        line.strip().startswith("--session ") for line in result.stdout.splitlines()
    ):
        raise LaunchError(
            "This Codex launch requires tj _session --session; "
            "update the pinned CLI first"
        )


def connection(native: str, directory: Path) -> AbstractContextManager[Server]:
    return cast(
        AbstractContextManager[Server],
        hook_trust.AppServer(native, directory, timeout=30, existing=True),
    )


def start_options(directory: Path, arguments: list[str]) -> StartOptions:
    if len(arguments) < 3 or arguments[:2] != ["--cd", str(directory)]:
        raise LaunchError("Unsupported managed Codex start arguments")
    options, prompt = arguments[2:-1], arguments[-1]
    model, effort = None, None
    if options[:1] == ["--model"] and len(options) >= 2:
        model, options = options[1], options[2:]
    if options[:1] == ["-c"] and len(options) == 2:
        key, separator, effort = options[1].partition("=")
        if key != "model_reasoning_effort" or not separator or not effort:
            raise LaunchError("Unsupported managed Codex reasoning effort")
        options = []
    if options or not prompt:
        raise LaunchError("Unsupported managed Codex start arguments")
    return StartOptions(model, effort, prompt)


def admit(directory: Path, arguments: list[str]) -> None:
    root = find_package_root(Path(__file__).resolve().parent)
    script = root / ".agents" / "agent" / "runtime" / "tj_prepare.py"
    prepared = process.run(
        [
            sys.executable,
            "-B",
            str(script),
            "--host",
            "codex",
            "--cwd",
            str(directory),
            "--arguments-stdin",
        ],
        input=json.dumps(arguments),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if prepared.returncode:
        raise LaunchError(
            f"Codex repository admission failed: {prepared.stderr.strip()}"
        )


def create(
    server: Server, directory: Path, title: str, model: str | None, effort: str | None
) -> str:
    parameters: dict[str, object] = {
        "cwd": str(directory),
        "historyMode": "paginated",
        "threadSource": "user",
        "ephemeral": False,
    }
    if model is not None:
        parameters["model"] = model
    if effort is not None:
        parameters["config"] = {"model_reasoning_effort": effort}
    response = server.request("thread/start", parameters)
    thread = response.get("thread")
    thread_id = native_id(thread.get("id") if isinstance(thread, dict) else None)
    completed = False
    try:
        if (
            (model is not None and response.get("model") != model)
            or (effort is not None and response.get("reasoningEffort") != effort)
            or response.get("cwd") != str(directory)
        ):
            raise LaunchError("Codex created a thread with different launch settings")
        server.request("thread/name/set", {"threadId": thread_id, "name": title})
        completed = True
        return thread_id
    finally:
        if not completed:
            cleanup_failed_launch(
                sys.exception(),
                lambda: server.request("thread/archive", {"threadId": thread_id}),
            )


def daemon_command(
    native: str, action: str, directory: Path, environ: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    return process.run(
        [native, "app-server", "daemon", action],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=daemon_environment(environ),
    )


def start_daemon(
    native: str,
    directory: Path,
    environ: Mapping[str, str],
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] = time.sleep,
) -> None:
    """Start the shared Codex daemon, from a terminal tab if this job forbids it."""
    started = daemon_command(native, "start", directory, environ)
    if not started.returncode:
        return
    if not IS_WINDOWS or JOB_REFUSAL not in started.stderr:
        raise LaunchError(
            f"Cannot start the configured Codex daemon: {started.stderr.strip()}"
        )
    terminal = shutil.which("wt.exe")
    if not terminal:
        raise LaunchError(
            "Cannot start the Codex daemon here and wt.exe is not on PATH"
        )
    # Run through cmd with output to NUL: as the tab's own process, the daemon
    # Codex spawns dies when the tab closes.
    process.run(
        [
            terminal,
            "-w",
            "0",
            "nt",
            "--title",
            "Codex daemon",
            "cmd.exe",
            "/d",
            "/c",
            native,
            "app-server",
            "daemon",
            "start",
            ">",
            "NUL",
            "2>&1",
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=daemon_environment(environ),
    )
    deadline = clock() + DAEMON_WAIT_SECONDS
    while daemon_command(native, "version", directory, environ).returncode:
        if clock() >= deadline:
            raise LaunchError(
                "The Codex daemon did not start from a Windows Terminal tab "
                f"within {DAEMON_WAIT_SECONDS} seconds"
            )
        wait(1)


def prepare(
    directory: Path,
    title: str,
    arguments: list[str],
    cli: str,
    native: str,
    environ: Mapping[str, str],
) -> PreparedLaunch:
    prepared = None
    completed = False
    try:
        require_binding(cli)
        resuming = (
            arguments[:3] == ["--cd", str(directory), "resume"] and len(arguments) == 4
        )
        thread_id = native_id(arguments[3]) if resuming else None
        options = None if resuming else start_options(directory, arguments)
        admit(directory, arguments)
        if daemon.disabled(projects.user_config()):
            raise LaunchError(
                "Codex shared daemon startup is disabled in the user configuration"
            )
        start_daemon(native, directory, environ)
        with connection(native, directory) as server:
            document = server.request(
                "config/read", {"cwd": str(directory), "includeLayers": True}
            )
            permissions.require_project_trust(
                document, directory, windows=os.name == "nt", wsl=inside_wsl()
            )
            if options is None:
                require_unloaded(server, str(thread_id))
                prepared = PreparedLaunch(
                    (native, *arguments, "--remote", "unix://"), f"codex:{thread_id}"
                )
            else:
                thread_id = create(
                    server, directory, title, options.model, options.effort
                )
                prepared = PreparedLaunch(
                    (
                        native,
                        "--cd",
                        str(directory),
                        "resume",
                        thread_id,
                        *arguments[2:-1],
                        "--remote",
                        "unix://",
                        "--",
                        options.prompt,
                    ),
                    f"codex:{thread_id}",
                    created=True,
                )
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as error:
        raise LaunchError(
            f"Cannot prepare exact Codex session binding: {error}"
        ) from error
    else:
        completed = True
        return prepared
    finally:
        if prepared is not None and not completed:
            cleanup_failed_launch(sys.exception(), lambda: abandon(prepared, "", False))


def require_unloaded(server: Server, thread_id: str) -> None:
    cursor = None
    while True:
        page = server.request("thread/loaded/list", {"cursor": cursor})
        loaded = page.get("data")
        if not isinstance(loaded, list):
            raise LaunchError("Codex returned an invalid loaded thread list")
        if thread_id in loaded:
            raise LaunchError(
                f"Codex {thread_id} is already loaded; use its existing conversation"
            )
        cursor = page.get("nextCursor")
        if not cursor:
            return


def first_turn(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    read_records = cast(
        Callable[[Path], Iterator[tuple[int, dict[str, object]]]], transcripts.records
    )
    for _number, event in read_records(path):
        if event.get("type") == "turn_context":
            payload = event.get("payload")
            if not isinstance(payload, dict):
                raise LaunchError("Codex recorded an invalid first turn")
            return cast(dict[str, object], payload)
    return None


def confirm(
    prepared: PreparedLaunch,
    arguments: list[str],
    launch_id: str,
    *,
    timeout: float = 180,
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] = time.sleep,
) -> None:
    if not prepared.created:
        try:
            confirm_resume(prepared, launch_id, timeout=timeout, clock=clock, wait=wait)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as error:
            raise LaunchError(
                f"Cannot verify Codex daemon attachment for {prepared.session}: {error}"
            ) from error
        return
    directory = Path(prepared.command[2])
    options = start_options(directory, arguments)
    thread_id = native_id(str(prepared.session).removeprefix("codex:"))
    deadline = clock() + timeout
    try:
        with connection(prepared.command[0], directory) as server:
            response = server.request(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            )
        thread = response.get("thread")
        if (
            not isinstance(thread, dict)
            or thread.get("id") != thread_id
            or not isinstance(thread.get("path"), str)
        ):
            raise LaunchError(
                "Codex did not supply the prepared session's rollout path"
            )
        path = Path(thread["path"])
        while (turn := first_turn(path)) is None:
            try:
                shown = agent_store.run("show", "--launch-id", launch_id)
            except agent_store.AgentStoreError as error:
                raise LaunchError(f"Cannot read launch {launch_id}: {error}") from error
            record = shown.get("launch")
            if not isinstance(record, dict) or record.get("launch_id") != launch_id:
                raise LaunchError(
                    f"agent-store returned an invalid launch for {launch_id}"
                )
            if record.get("state") == "ended":
                # The turn can be written between the first read and the store check.
                if (turn := first_turn(path)) is not None:
                    break
                detail = ""
                try:
                    with connection(prepared.command[0], directory) as server:
                        server.request(
                            "thread/read", {"threadId": thread_id, "includeTurns": True}
                        )
                except (OSError, ValueError, RuntimeError) as error:
                    detail = f"; Codex thread/read: {error}"
                raise LaunchError(
                    f"Codex launch {launch_id} exited before its first turn"
                    f" (reason: {record.get('end_reason')}){detail}"
                )
            if clock() >= deadline:
                raise LaunchTimeout(
                    f"Codex {thread_id} did not record its first turn within "
                    f"{timeout:g} seconds; check its tab"
                )
            wait(0.5)
    except (OSError, ValueError, RuntimeError) as error:
        raise LaunchError(
            f"Cannot verify Codex first turn for {thread_id}: {error}"
        ) from error
    mode = turn.get("collaboration_mode")
    settings = mode.get("settings") if isinstance(mode, dict) else None
    effort = settings.get("reasoning_effort") if isinstance(settings, dict) else None
    if (options.model is not None and turn.get("model") != options.model) or (
        options.effort is not None and effort != options.effort
    ):
        raise LaunchError(
            f"Codex {thread_id} first turn used model={turn.get('model')!r}, "
            f"effort={effort!r}; requested model={options.model!r}, "
            f"effort={options.effort!r}. Check its tab before continuing"
        )
    try:
        with connection(prepared.command[0], directory) as server:
            response = server.request(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            )
    except (OSError, ValueError, RuntimeError) as error:
        raise LaunchError(
            f"Cannot verify Codex daemon attachment for {thread_id}: {error}"
        ) from error
    thread = response.get("thread")
    status = thread.get("status") if isinstance(thread, dict) else None
    if (
        not isinstance(thread, dict)
        or thread.get("id") != thread_id
        or not isinstance(status, dict)
        or status.get("type") not in {"idle", "active"}
    ):
        raise LaunchError(
            f"Codex {thread_id} first turn is not attached to the shared daemon; "
            "check its tab"
        )


def confirm_resume(
    prepared: PreparedLaunch,
    launch_id: str,
    *,
    timeout: float,
    clock: Callable[[], float],
    wait: Callable[[float], None],
) -> None:
    thread_id = native_id(str(prepared.session).removeprefix("codex:"))
    deadline = clock() + timeout
    with connection(prepared.command[0], Path(prepared.command[2])) as server:
        while True:
            response = server.request(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            )
            thread = response.get("thread")
            if not isinstance(thread, dict) or thread.get("id") != thread_id:
                raise LaunchError(
                    f"Codex did not return the resumed thread {thread_id}"
                )
            status = thread.get("status")
            if isinstance(status, dict) and status.get("type") in {"idle", "active"}:
                return
            shown = agent_store.run("show", "--launch-id", launch_id)
            record = shown.get("launch")
            if not isinstance(record, dict) or record.get("launch_id") != launch_id:
                raise LaunchError(
                    f"agent-store returned an invalid launch for {launch_id}"
                )
            if record.get("state") == "ended":
                raise LaunchError(
                    f"Codex launch {launch_id} exited before attaching "
                    "to the shared daemon"
                )
            if clock() >= deadline:
                raise LaunchTimeout(
                    f"Codex {thread_id} did not attach to the shared daemon "
                    f"within {timeout:g} seconds; check its tab"
                )
            wait(0.5)


def not_started(
    prepared: PreparedLaunch, launch_id: str, document: Mapping[str, object]
) -> bool:
    record, session = document.get("launch"), document.get("session")
    if not isinstance(record, dict):
        return False
    wrapper = record.get("wrapper")
    if (
        record.get("launch_id") != launch_id
        or record.get("host") != "codex"
        or record.get("state") != "ended"
        or record.get("end_reason") != "not_started"
        or not isinstance(wrapper, dict)
        or not isinstance(wrapper.get("pid"), int)
        or isinstance(wrapper["pid"], bool)
        or wrapper["pid"] <= 0
        or not isinstance(wrapper.get("started_at"), str)
        or not wrapper["started_at"]
    ):
        return False
    if record.get("session") is None:
        return session is None
    return (
        isinstance(session, dict)
        and record.get("session") == prepared.session
        and session.get("session") == prepared.session
        and session.get("host") == "codex"
        and session.get("native_id") == prepared.command[4]
        and session.get("launch_id") == launch_id
        and session.get("wrapper") == wrapper
    )


def abandon(prepared: PreparedLaunch, launch_id: str, dispatched: bool) -> None:
    if not prepared.created:
        return
    if dispatched:
        try:
            document = agent_store.run("show", "--launch-id", launch_id)
        except agent_store.AgentStoreError as error:
            if error.code == "not-found":
                return
            raise
        if not not_started(prepared, launch_id, document):
            return
        if document.get("session") is not None:
            reciprocal = agent_store.run("show", str(prepared.session))
            if not not_started(prepared, launch_id, reciprocal):
                return
    with connection(prepared.command[0], Path(prepared.command[2])) as server:
        server.request("thread/archive", {"threadId": prepared.command[4]})
    if dispatched and document.get("session") is not None:
        agent_store.run(
            "end", "--session", str(prepared.session), "--reason", "not_started"
        )
