"""The cli-sessions registry entries the session hooks keep in step with the store (Python 3.9+)."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time


REPLACE_ATTEMPTS = 40
REPLACE_DELAY_SECONDS = 0.05


@dataclass(frozen=True)
class CliSessionRegistry:
    directory: Path

    def follow_cwd(self, host, session_id, cwd):
        path = Path(self.directory) / f"{session_id}.json"
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (not isinstance(entry, dict) or entry.get("session_id") != session_id
                or entry.get("host") not in (None, host) or entry.get("cwd") == str(cwd)):
            return None
        entry.setdefault("launch_cwd", entry.get("cwd"))
        entry["cwd"] = str(cwd)
        staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            staging.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
            for _attempt in range(REPLACE_ATTEMPTS):
                try:
                    os.replace(staging, path)
                    return True
                except PermissionError:
                    time.sleep(REPLACE_DELAY_SECONDS)
            return False
        except OSError:
            return False
        finally:
            staging.unlink(missing_ok=True)
