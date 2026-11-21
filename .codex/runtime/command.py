"""The installed Codex foreground command entry point."""

import os
import sys
from pathlib import Path


def shared_runtime() -> Path:
    for root in Path(__file__).resolve().parents:
        runtime = root / ".agents" / "agent" / "runtime"
        if runtime.is_dir():
            return runtime
    raise RuntimeError("The bundled Agent runtime was not found")


sys.path.insert(0, str(shared_runtime()))  # noqa: TID251

from host_command import main as run_host  # noqa: E402
from tj_agent.host import Host  # noqa: E402


def main() -> int:
    return run_host(Host.CODEX, sys.argv[1:], cwd=Path.cwd(), environ=os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
