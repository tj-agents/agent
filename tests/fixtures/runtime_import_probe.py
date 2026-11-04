"""Load an entry point using only its shipped, explicitly located dependencies."""

import importlib
import importlib.util
import json
import runpy
import sys
from pathlib import Path

target = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(target.parent))
if target.name == "checkout.py":
    spec = importlib.util.spec_from_file_location("isolated_checkout", target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
else:
    runpy.run_path(str(target))

process = importlib.import_module("tj_cli.process")
assert process.__file__ is not None

print(json.dumps({"runtime": str(Path(process.__file__).resolve())}))
