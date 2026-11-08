import json
import os
import sys
import time
from pathlib import Path


socket = Path(sys.argv[sys.argv.index("--listen") + 1].removeprefix("unix://"))
socket.parent.mkdir()
socket.write_bytes(b"owned fixture")
record = Path(os.environ["OWNED_FIXTURE_DIRECTORY"]) / "native.json"
temporary = record.with_suffix(".tmp")
temporary.write_text(
    json.dumps({"pid": os.getpid(), "cwd": str(Path.cwd())}), encoding="utf-8"
)
temporary.replace(record)
while True:
    time.sleep(0.1)
