"""Open a prepared handoff with the installed managed runtime."""

import argparse
import runpy
import sys
from collections.abc import Sequence
from pathlib import Path

from package_root import find_package_root
from tj_agent.host import Host


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tj handoff", description=__doc__)
    parser.add_argument("--host", choices=[host.value for host in Host], required=True)
    values = list(sys.argv[1:] if argv is None else argv)
    boundary = values.index("--") if "--" in values else len(values)
    host = parser.parse_args(values[:boundary]).host
    root = find_package_root(Path(__file__).resolve().parent)
    launcher = root / ".agents/agent/resources/handoff" / f"launch_{host}.py"
    return int(runpy.run_path(str(launcher))["main"](values[boundary + 1 :]))


if __name__ == "__main__":
    raise SystemExit(main())
