"""Start the shared Codex update observer from the session-start hook."""

import subprocess
import sys
from pathlib import Path

from codex import observer
from tj_agent.host import Host


def main() -> int:
    try:
        observer.ensure(Host.CODEX.configuration_root, Path.home())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Codex update observer: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
