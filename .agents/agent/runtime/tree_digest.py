"""Plugin tree digests that re-hash a file only when its size or mtime changes."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

CACHE = "plugin-digests.json"
IGNORED = (".in_use", "__pycache__")


class Digests:
    def __init__(self, state: Path) -> None:
        self.path = state / CACHE
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        self.files: dict[str, list[object]] = loaded if isinstance(loaded, dict) else {}
        self.changed = False

    def file(self, path: Path) -> str:
        status = path.stat()
        known = self.files.get(str(path))
        if (
            isinstance(known, list)
            and len(known) == 3
            and known[:2] == [status.st_size, status.st_mtime_ns]
            and isinstance(known[2], str)
            and re.fullmatch("[0-9a-f]{64}", known[2]) is not None
        ):
            return known[2]
        digest = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        self.files[str(path)] = [status.st_size, status.st_mtime_ns, digest]
        self.changed = True
        return digest

    def tree(self, root: Path) -> str | None:
        if not root.is_dir():
            return None
        files = {}
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if (
                relative.parts[0] in IGNORED
                or "__pycache__" in relative.parts
                or not path.is_file()
            ):
                continue
            files[relative.as_posix()] = self.file(path)
        return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()

    def save(self) -> None:
        if self.changed:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.files), encoding="utf-8")
            self.changed = False
