"""Load shared process execution from this Agent package's shipped runtime."""

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
        else:
            runtime = root / ".agents" / "agent" / "runtime"
        if (runtime / "tj_cli" / "process" / "__init__.py").is_file():
            return runtime
    raise RuntimeError("The shipped Agent process runtime was not found")


_RUNTIME = _runtime()
if str(_RUNTIME) not in sys.path:
    sys.path.insert(0, str(_RUNTIME))
process = importlib.import_module("tj_cli.process")

__all__ = ["process"]
