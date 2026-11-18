"""Run a native host CLI as an independent managed foreground session."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import agent_cli
import host_plugins
import required_plugins
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


def native_executable(host: Host, resolver: Callable[[], str] | None = None) -> str:
    """Resolve the host's native executable to the absolute path passed to tj."""
    resolved = (
        host_plugins.PLUGINS[host].resolve_executable()
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
        executable = native_executable(host, resolver)
        effective_directory, _inventory_options = required_plugins.launch_context(
            directory, host, values
        )
        if effective_directory is not None:
            values = agent_cli.host_launch(host).managed_arguments(values)
    except (LaunchError, OSError, required_plugins.RequiredPluginsError) as error:
        print(f"tj {host}: {error}", file=sys.stderr)
        return 1

    if effective_directory is None:
        return run_native([executable, *values], directory, environment, execute)

    return run_native(
        [
            "tj",
            "_session",
            "--host",
            host.value,
            "--cwd",
            str(effective_directory),
            "--command-cwd",
            str(directory),
            "--",
            executable,
            *values,
        ],
        directory,
        environment,
        execute,
    )
