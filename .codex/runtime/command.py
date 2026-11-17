"""The installed Codex foreground command entry point."""

import sys
from pathlib import Path


def shared_runtime() -> Path:
    for root in Path(__file__).resolve().parents:
        runtime = root / ".agents" / "agent" / "runtime"
        if runtime.is_dir():
            return runtime
    raise RuntimeError("The bundled Agent runtime was not found")


sys.path.insert(0, str(shared_runtime()))  # noqa: TID251

from host_command import main  # noqa: E402, I001
from tj_agent.host import Host  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(Host.CODEX))
