import runpy
import sys
from pathlib import Path


here = Path(__file__).resolve()
if sys.path and Path(sys.path[0]).resolve() == here.parent:
    sys.path.pop(0)
for declared in here.parents:
    if any(
        (declared / marker).is_file()
        for marker in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json")
    ):
        boundary = declared / "python"
        if not (boundary / "tj_cli" / "__init__.py").is_file():
            raise RuntimeError("declared tj_cli import boundary is unavailable")
        sys.path.insert(0, str(boundary))
        break
    if (declared / "pyproject.toml").is_file():
        break
else:
    raise RuntimeError("declared package boundary is unavailable")

if __name__ == "__main__":
    runpy.run_module("tj_cli.terminal.settings", run_name="__main__")
