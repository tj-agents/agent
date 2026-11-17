"""Exercise an installed host wrapper with Python as the provider executable."""

import importlib
import runpy
import sys
from pathlib import Path
from unittest import mock


def main() -> None:
    runtime, name, script = sys.argv[1:]
    sys.path.insert(0, runtime)  # noqa: TID251
    plugins = importlib.import_module("host_plugins")
    host = next(host for host in plugins.PLUGINS if host.value == name)
    entry = Path(runtime) / script
    sys.argv = [str(entry), "--help"]
    with mock.patch.object(
        plugins.PLUGINS[host], "resolve_executable", return_value=sys.executable
    ):
        runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__":
    main()
