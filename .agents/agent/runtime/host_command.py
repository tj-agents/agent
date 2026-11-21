"""Run a native host CLI as an independent managed foreground session."""

from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import agent_cli
import host_plugins
import required_plugins
import session_resume
from launch_errors import LaunchError
from tj_agent.host import Host


def independent_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Remove ambient TJ and native session identities while retaining host settings."""
    result = dict(environ)
    for name in tuple(result):
        if name.startswith("TJ_RUN_"):
            result.pop(name)
    for host in Host:
        result.pop(host.session_variable, None)
    return result


def native_executable(
    host: Host, resolver: Callable[[], str] | None = None, *, maintenance: bool = False
) -> str:
    """Resolve the host's native executable to the absolute path passed to tj."""
    resolved = (
        host_plugins.PLUGINS[host].resolve_executable(maintenance=maintenance)
        if resolver is None
        else resolver()
    )
    return str(Path(resolved).resolve())


def run_native(
    command: Sequence[str],
    directory: Path,
    environ: Mapping[str, str],
    run: Callable[..., subprocess.CompletedProcess[object]],
) -> int:
    try:
        completed = run(command, cwd=directory, env=environ, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"tj: could not start {command[0]}: {error}", file=sys.stderr)
        return 1
    return completed.returncode


def main(
    host: Host,
    arguments: Sequence[str],
    *,
    cwd: Path,
    environ: Mapping[str, str],
    resolver: Callable[[], str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[object]] | None = None,
) -> int:
    """Run a native command directly or through one foreground managed session."""
    values = tuple(arguments)
    directory = cwd.resolve()
    environment = independent_environment(environ)
    execute = subprocess.run if run is None else run
    try:
        effective_directory, _inventory_options = required_plugins.launch_context(
            directory, host, values
        )
        executable = native_executable(
            host, resolver, maintenance=effective_directory is None
        )
        if effective_directory is None:
            return run_native([executable, *values], directory, environment, execute)
        cleared, forced = agent_cli.terminal_environment(host)
        for name in cleared:
            environment.pop(name, None)
        environment.update(forced)
        adapter = agent_cli.host_launch(host)
        prepared = adapter.prepare_foreground(
            executable, effective_directory, values, "tj", environment
        )
        predecessor = None
        if prepared.session and not prepared.created:
            predecessor = session_resume.predecessor(prepared.session)
            if predecessor is not None:
                session_resume.check(prepared.session, predecessor)
    except (LaunchError, OSError, required_plugins.RequiredPluginsError) as error:
        print(f"tj {host}: {error}", file=sys.stderr)
        return 1

    launch_id = uuid.uuid4().hex
    command = [
        "tj",
        "_session",
        "--host",
        host.value,
        "--cwd",
        str(effective_directory),
        "--command-cwd",
        str(directory),
    ]
    if prepared.session:
        command.extend(("--session", prepared.session, "--launch-id", launch_id))
    if predecessor:
        command.extend(("--resume-from", predecessor))
    result = run_native(
        [*command, "--", *prepared.command], directory, environment, execute
    )
    if result and prepared.created:
        try:
            adapter.abandon(prepared, launch_id, dispatched=True)
        except (
            LaunchError,
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as error:
            print(
                f"tj {host}: cleanup for launch {launch_id} failed: {error}",
                file=sys.stderr,
            )
    return result
