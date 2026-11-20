from __future__ import annotations

import json
import os
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
from codex import hook_trust, permissions, processes
from launch_contract import Confirmation, PreparedLaunch
from launch_errors import LaunchError, LaunchTimeout, cleanup_failed_launch
from package_root import find_package_root
from tj_cli import process

MINIMUM_VERSION = "0.169.0"
MANAGED_OPTIONS = ("--remote", "unix://")


class Server(Protocol):
    def request(self, method: str, params: dict[str, object]) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class StartOptions:
    model: str | None
    effort: str | None
    prompt: str | None


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


def connection(
    native: str,
    directory: Path,
    *,
    existing: bool = False,
    environment: Mapping[str, str] | None = None,
) -> AbstractContextManager[Server]:
    return cast(
        AbstractContextManager[Server],
        hook_trust.AppServer(
            native, directory, timeout=30, existing=existing, environment=environment
        ),
    )


def start_options(directory: Path, arguments: list[str]) -> StartOptions:
    if arguments[:2] != ["--cd", str(directory)]:
        raise LaunchError("Unsupported managed Codex start arguments")
    options = arguments[2:]
    model, effort = None, None
    if options[:1] == ["--model"] and len(options) >= 2:
        model, options = options[1], options[2:]
    if options[:1] == ["-c"] and len(options) >= 2:
        key, separator, effort = options[1].partition("=")
        if key != "model_reasoning_effort" or not separator or not effort:
            raise LaunchError("Unsupported managed Codex reasoning effort")
        options = options[2:]
    if len(options) > 1 or options == [""]:
        raise LaunchError("Unsupported managed Codex start arguments")
    return StartOptions(model, effort, options[0] if options else None)


def admit(directory: Path, arguments: list[str], environ: Mapping[str, str]) -> None:
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
        env=environ,
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
    config: dict[str, object] = {}
    parameters["config"] = config
    if model is not None:
        parameters["model"] = model
    if effort is not None:
        config["model_reasoning_effort"] = effort
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
        admit(directory, arguments, environ)
        with connection(native, directory, environment=environ) as server:
            document = server.request(
                "config/read", {"cwd": str(directory), "includeLayers": True}
            )
            permissions.require_project_trust(
                document, directory, windows=os.name == "nt", wsl=inside_wsl()
            )
            if options is None:
                existing = server.request(
                    "thread/read", {"threadId": thread_id, "includeTurns": False}
                ).get("thread")
                if not isinstance(existing, dict) or existing.get("id") != thread_id:
                    raise LaunchError(
                        "Codex did not return the requested existing thread"
                    )
                prepared = PreparedLaunch(
                    (native, *arguments, *MANAGED_OPTIONS), f"codex:{thread_id}"
                )
            else:
                thread_id = create(
                    server, directory, title, options.model, options.effort
                )
                native_options = (
                    arguments[2:-1] if options.prompt is not None else arguments[2:]
                )
                prepared = PreparedLaunch(
                    (
                        native,
                        "--cd",
                        str(directory),
                        "resume",
                        thread_id,
                        *native_options,
                        *MANAGED_OPTIONS,
                        *(("--", options.prompt) if options.prompt is not None else ()),
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
) -> Confirmation:
    if not prepared.created:
        await_native_process(
            prepared, launch_id, timeout=timeout, clock=clock, wait=wait
        )
        await_daemon_thread(prepared, timeout=timeout, clock=clock, wait=wait)
        return Confirmation.PROCESS_STARTED
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
    await_native_process(prepared, launch_id, timeout=timeout, clock=clock, wait=wait)
    await_daemon_thread(prepared, timeout=timeout, clock=clock, wait=wait)
    return Confirmation.FIRST_TURN


def await_daemon_thread(
    prepared: PreparedLaunch,
    *,
    timeout: float,
    clock: Callable[[], float],
    wait: Callable[[float], None],
) -> None:
    thread_id = native_id(str(prepared.session).removeprefix("codex:"))
    directory = Path(prepared.command[2])
    deadline = clock() + timeout
    try:
        with connection(prepared.command[0], directory, existing=True) as server:
            while True:
                response = server.request(
                    "thread/read", {"threadId": thread_id, "includeTurns": False}
                )
                thread = response.get("thread")
                if (
                    not isinstance(thread, dict)
                    or thread.get("id") != thread_id
                    or thread.get("cwd") != str(directory)
                ):
                    raise LaunchError(
                        "Codex daemon returned a different thread or checkout"
                    )
                status = thread.get("status")
                if isinstance(status, dict) and status.get("type") in {
                    "idle",
                    "active",
                }:
                    return
                if clock() >= deadline:
                    raise LaunchTimeout(
                        f"Codex {thread_id} did not attach to the managed local daemon"
                    )
                wait(0.5)
    except (OSError, ValueError, RuntimeError) as error:
        raise LaunchError(
            f"Cannot verify Codex daemon attachment for {thread_id}: {error}"
        ) from error


def await_native_process(
    prepared: PreparedLaunch,
    launch_id: str,
    *,
    timeout: float,
    clock: Callable[[], float],
    wait: Callable[[float], None],
) -> None:
    thread_id = native_id(str(prepared.session).removeprefix("codex:"))
    deadline = clock() + timeout
    while True:
        try:
            # Immutable observations retain liveness evidence after ownership changes.
            # Only a fresh reciprocal binding check can confirm this particular launch.
            current = processes.record_prepared(thread_id, launch_id)
            observed = processes.inventory(launch_id=launch_id).get(thread_id)
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as error:
            raise LaunchError(
                f"Cannot verify Codex native process for {prepared.session}: {error}"
            ) from error
        if current and observed is not None and observed.alive:
            return
        shown = agent_store.run("show", "--launch-id", launch_id)
        record = shown.get("launch")
        if not isinstance(record, dict) or record.get("launch_id") != launch_id:
            raise LaunchError(f"agent-store returned an invalid launch for {launch_id}")
        if record.get("state") == "ended":
            raise LaunchError(
                f"Codex launch {launch_id} exited before native process confirmation"
            )
        if clock() >= deadline:
            raise LaunchTimeout(
                f"Codex {thread_id} did not confirm its owned native process "
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
