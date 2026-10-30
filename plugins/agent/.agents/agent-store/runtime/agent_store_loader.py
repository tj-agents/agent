"""Loads an Agent Store runtime module from beside a given file, once per process (Python 3.9+)."""

import hashlib
import importlib.util
from pathlib import Path
import sys


def load_sibling(anchor, name):
    path = Path(anchor).resolve().with_name(f"{name}.py")
    key = f"{name}_{hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:16]}"
    module = sys.modules.get(key)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"The Agent Store {name}.py module was not found in {path.parent}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(key, None)
        raise
    return module
