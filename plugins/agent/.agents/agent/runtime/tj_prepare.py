import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import host_plugins
import required_plugins
import standards
from tj_agent.host import Host
from tj_cli import process
from tj_cli.git import repository_file

CONFIGURATION = Path(__file__).parent.parent / "utility/plugins/scripts/repo_config.py"


def configure(project: Path, mode: str) -> str:
    configured = process.run(
        [
            sys.executable,
            "-B",
            str(CONFIGURATION),
            "--root",
            str(project),
            "--mode",
            mode,
            "--preserve-permissions",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    drift = str(configured.stdout).strip()
    if configured.returncode and not (
        mode == "check" and drift.startswith("Drift in:")
    ):
        raise standards.StandardsError(configured.stderr.strip() or drift)
    return drift


def prepare(
    directory: Path, host: Host, arguments: tuple[str, ...] = (), *, write: bool = False
) -> bool:
    effective_directory, inventory_options = required_plugins.launch_context(
        directory, host, arguments
    )
    if effective_directory is None:
        return False
    directory = effective_directory
    project = required_plugins.project_root(directory)
    selected = required_plugins.selected(project)
    if not selected:
        return False
    with ThreadPoolExecutor(max_workers=1) as pool:
        inventory = pool.submit(
            required_plugins.check,
            directory,
            host,
            inventory_options=inventory_options,
        )
        configured = configure(project, "write" if write else "check")
    try:
        roots = (
            required_plugins.check(directory, host, inventory_options=inventory_options)
            if configured and write
            else inventory.result()
        )
    except required_plugins.MissingRequiredPluginsError as error:
        roots = required_plugins.install_and_recheck(
            directory, host, error, inventory_options=inventory_options
        )
    try:
        warning = host_plugins.PLUGINS[host].ensure_hooks(
            directory, required_roots=roots or {}
        )
    except RuntimeError as error:
        raise required_plugins.RequiredPluginsError(
            f"Required host hooks are not ready: {error}"
        ) from error
    if warning:
        raise required_plugins.RequiredPluginsError(warning)
    if repository_file(project, ".agents/repo-capabilities.local.json").is_file():
        if configured and not write:
            print(
                f"tj prepare: plugin configuration drift in {project}; run "
                f"tj prepare --host {host.value} --write. "
                "Local adoption preserves the repository's instruction files.",
                file=sys.stderr,
            )
        return bool(configured)
    instructions_changed = host_plugins.PLUGINS[Host.CLAUDE].prepare_instructions(
        project, write=write
    )
    if host != Host.CLAUDE:
        instructions_changed = (
            host_plugins.PLUGINS[host].prepare_instructions(project, write=write)
            or instructions_changed
        )
    changed = standards.update(project, roots or {}, check=not write)
    if not write and (configured or instructions_changed or changed):
        print(
            f"tj prepare: left {project} unchanged; from that checkout run "
            f"tj prepare --host {host.value} --write "
            "to apply the selected plugin configuration and instructions",
            file=sys.stderr,
        )
    return configured != "" or instructions_changed or changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tj prepare", description="Prepare repository standards before a session."
    )
    hosts = {host.value: host for host in Host}
    parser.add_argument("--host", choices=list(hosts), required=True)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--arguments-stdin", action="store_true")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Apply repository configuration and instruction updates explicitly.",
    )
    arguments = parser.parse_args(argv)
    try:
        host_arguments = (
            json.load(sys.stdin.buffer) if arguments.arguments_stdin else []
        )
        if not isinstance(host_arguments, list) or any(
            not isinstance(value, str) for value in host_arguments
        ):
            raise ValueError("Host arguments must be a JSON string array")
        prepare(
            arguments.cwd,
            hosts[arguments.host],
            tuple(host_arguments),
            write=arguments.write,
        )
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        required_plugins.RequiredPluginsError,
        standards.StandardsError,
    ) as error:
        print(f"tj prepare: standards warning: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
