"""Run one Agent module from its declared import boundary."""

import runpy
import sys
from pathlib import Path


def main() -> None:
    boundary = Path(__file__).resolve().parent.parent
    if not (boundary / "tj_agent" / "__init__.py").is_file():
        raise RuntimeError("Agent import boundary is unavailable")
    if boundary.name == "python":
        for package in ("tj_agent", "tj_agent_store", "tj_cli"):
            if not (boundary / package / "__init__.py").is_file():
                raise RuntimeError(f"declared {package} import boundary is unavailable")
    sys.path.insert(0, str(boundary))
    module, *arguments = sys.argv[1:]
    if not module.startswith("tj_agent."):
        raise ValueError("an Agent module is required")
    sys.argv = [module, *arguments]
    runpy.run_module(module, run_name="__main__")


if __name__ == "__main__":
    main()
