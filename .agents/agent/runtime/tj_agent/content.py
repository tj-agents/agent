from __future__ import annotations

import hashlib
from collections.abc import Mapping


def digest(files: Mapping[str, bytes]) -> str:
    value = hashlib.sha256()
    for name, body in sorted(files.items()):
        value.update(name.encode("utf-8") + b"\0" + str(len(body)).encode("ascii") + b"\0" + body)
    return value.hexdigest()
