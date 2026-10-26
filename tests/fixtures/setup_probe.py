"""Disposable setup entrypoint for the automatic recovery integration tests."""

import json
import os
from pathlib import Path
import sys

source = Path(__file__).resolve().parent
state = Path(os.environ["AGENT_STATE_DIRECTORY"])
version = (source / "revision").read_text(encoding="utf-8")
target = state / "installed-version"
if (source / "refuse").exists():
    raise SystemExit("fixture requires native credential enrollment")
if "--check" in sys.argv:
    raise SystemExit(0 if target.is_file() and target.read_text(encoding="utf-8") == version else 1)
if not target.is_file() or target.read_text(encoding="utf-8") != version:
    target.write_text(version, encoding="utf-8")
    count = state / "setup-applications"
    count.write_text(str(int(count.read_text()) + 1) if count.exists() else "1", encoding="utf-8")
channel = state / "machine-events.json"
expected = json.dumps({"topic": "a" * 48})
if not channel.exists() or channel.read_text(encoding="utf-8") != expected:
    channel.write_text(expected, encoding="utf-8")
