"""Inspect native executable candidates without invoking them."""

import os
from pathlib import Path


def is_script(path: str | os.PathLike[str]) -> bool:
    try:
        with Path(path).open("rb") as executable:
            return executable.read(2) == b"#!"
    except OSError:
        return True
