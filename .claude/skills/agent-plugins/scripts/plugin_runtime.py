"""Load shared process execution and the host model from this Agent package's shipped runtime."""

import importlib
import sys
from pathlib import Path


def _runtime() -> Path:
    here = Path(__file__).resolve().parent
    if (here / "tj_cli" / "process" / "__init__.py").is_file():
        return here
    for root in Path(__file__).resolve().parents:
        if (root / ".agents" / "plugins" / "payloads.json").is_file():
            runtime = root / "plugins" / "agent" / ".agents" / "agent" / "runtime"
        elif any(
            (root / marker).exists()
            for marker in (
                ".git",
                ".codex-plugin/plugin.json",
                ".claude-plugin/plugin.json",
                "source.json",
            )
        ):
            runtime = root / ".agents" / "agent" / "runtime"
        else:
            continue
        if (runtime / "tj_cli" / "process" / "__init__.py").is_file():
            return runtime
        raise RuntimeError(
            f"The shipped Agent process runtime is unavailable at {runtime}"
        )
    raise RuntimeError("The shipped Agent process runtime was not found")


_RUNTIME = _runtime()
if str(_RUNTIME) not in sys.path:
    sys.path.insert(0, str(_RUNTIME))
process = importlib.import_module("tj_cli.process")
Host = importlib.import_module("tj_agent.host").Host

__all__ = ["Host", "process"]
