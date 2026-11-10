import argparse
import json
import subprocess
import sys
from pathlib import Path

import host_plugins
import required_plugins
import standards
from tj_agent.host import Host
from tj_cli import process


def prepare(directory: Path, host: Host, arguments: tuple[str, ...] = ()) -> bool:
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
    script = Path(__file__).parent.parent / "utility/plugins/scripts/repo_config.py"
    configured = process.run(
        [sys.executable, "-B", str(script), "--root", str(project), "--mode", "write"],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    if configured.returncode:
        raise standards.StandardsError(
            configured.stderr.strip() or configured.stdout.strip()
        )
    try:
        roots = required_plugins.check(
            directory, host, inventory_options=inventory_options
        )
    except required_plugins.MissingRequiredPluginsError as error:
        host_plugins.PLUGINS[host].install(project, error.missing)
        roots = required_plugins.check(
            directory, host, inventory_options=inventory_options
        )
    if (project / ".agents/repo-capabilities.local.json").is_file():
        raise standards.StandardsError(
            "Local-only adoption preserves this repository's instruction files; "
            "its owner must supply standards routing."
        )
    host_plugins.PLUGINS[Host.CLAUDE].prepare_instructions(project)
    if host != Host.CLAUDE:
        host_plugins.PLUGINS[host].prepare_instructions(project)
    return standards.update(project, roots or {})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tj prepare", description="Prepare repository standards before a session."
    )
    parser.add_argument("--host", type=Host, choices=list(Host), required=True)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--arguments-stdin", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        host_arguments = json.load(sys.stdin) if arguments.arguments_stdin else []
        if not isinstance(host_arguments, list) or any(
            not isinstance(value, str) for value in host_arguments
        ):
            raise ValueError("Host arguments must be a JSON string array")
        prepare(arguments.cwd, arguments.host, tuple(host_arguments))
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
