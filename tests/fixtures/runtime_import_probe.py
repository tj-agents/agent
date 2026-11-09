"""Load an entry point using only its shipped, explicitly located dependencies."""

import importlib
import json
import runpy
import sys
from pathlib import Path

target = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(target.parent))
runpy.run_path(str(target))

process = importlib.import_module("tj_cli.process")
assert process.__file__ is not None

print(json.dumps({"runtime": str(Path(process.__file__).resolve())}))
