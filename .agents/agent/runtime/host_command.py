"""Run a native host CLI as an independent managed foreground session."""

from __future__ import annotations

import argparse
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
        host_plugins.PLUGINS[host].resolve_executable(
            maintenance=maintenance, start_if_absent=not maintenance
        )
        if resolver is None
        else resolver()
    )
    return str(Path(resolved).resolve())


def run_native(
    command: Sequence[str],
    directory: Path,
    environ: Mapping[str, str],
    run: Callable[..., subprocess.CompletedProcess[object]],
    *,
    maintenance_timeout: float | None = None,
) -> int:
    try:
        if maintenance_timeout is None:
            completed = run(command, cwd=directory, env=environ, check=False)
        else:
            completed = run(
                command,
                cwd=directory,
                env=environ,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=sys.stderr,
                timeout=maintenance_timeout,
            )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"tj: could not start {command[0]}: {error}", file=sys.stderr)
        return 1
    return completed.returncode


def run_prepared_tab(
    host: Host,
    values: Sequence[str],
    directory: Path,
    environment: Mapping[str, str],
    resolver: Callable[[], str] | None,
    execute: Callable[..., subprocess.CompletedProcess[object]],
) -> int:
    """Run an already prepared native session in the newly opened foreground."""
    if "--" not in values:
        raise LaunchError("A prepared tab requires its native command after --")
    boundary = values.index("--")
    metadata = values[:boundary]
    native = values[boundary + 1 :]
    parser = argparse.ArgumentParser(prog=f"tj {host} --tj-tab", allow_abbrev=False)
    for key in ("title", "cwd", "launch-id"):
        parser.add_argument(f"--{key}", required=True)
    for key in ("parent", "item", "session", "resume-from"):
        parser.add_argument(f"--{key}")
    options = parser.parse_args(metadata)
    if Path(options.cwd).resolve() != directory:
        raise LaunchError("The prepared tab opened in a different directory")
    executable = native_executable(host, resolver)
    if not native or Path(native[0]).resolve() != Path(executable):
        raise LaunchError("The selected host executable changed during tab startup")
    arguments = agent_cli.host_launch(host).managed_arguments(native[1:])
    return run_native(
        [
            "tj",
            "_session",
            "--host",
            host.value,
            *metadata,
            "--",
            executable,
            *arguments,
        ],
        directory,
        environment,
        execute,
    )


def main(
    host: Host,
    arguments: Sequence[str],
    *,
    cwd: Path,
    environ: Mapping[str, str],
    resolver: Callable[[], str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[object]] | None = None,
    maintenance_timeout: float | None = None,
) -> int:
    """Run a native command directly or through one foreground managed session."""
    values = tuple(arguments)
    directory = cwd.resolve()
    environment = independent_environment(environ)
    execute = subprocess.run if run is None else run
    try:
        if values[:1] == ("--tj-tab",):
            return run_prepared_tab(
                host, values[1:], directory, environment, resolver, execute
            )
        effective_directory, _inventory_options = required_plugins.launch_context(
            directory, host, values
        )
        executable = native_executable(
            host, resolver, maintenance=effective_directory is None
        )
        if effective_directory is None:
            return run_native(
                [executable, *values],
                directory,
                environment,
                execute,
                maintenance_timeout=maintenance_timeout,
            )
        adapter = agent_cli.host_launch(host)
        if sys.stdin.isatty() and sys.stdout.isatty():
            terminal = adapter.TERMINAL_ENVIRONMENT
            environment = agent_cli.environment_for(
                environment,
                terminal.cleared(windows=agent_cli.IS_WINDOWS),
                terminal.forced(windows=agent_cli.IS_WINDOWS),
            )
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
