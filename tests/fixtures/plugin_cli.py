"""A native-process plugin installer with durable dependency checks for integration tests."""

import json
import os
from pathlib import Path
import sys

home = Path(os.environ["CLAUDE_CONFIG_DIR"])
path = home / "plugins/installed_plugins.json"
document = json.loads(path.read_text()) if path.exists() else {"plugins": {}}
entries = document["plugins"]
arguments = sys.argv[1:]
if arguments[:2] == ["plugin", "list"]:
    print(json.dumps([{**entry, "id": identity} for identity, rows in entries.items() for entry in rows]))
    raise SystemExit(0)
operation, identity = arguments[1:3]
scope = arguments[arguments.index("--scope") + 1]
catalog = json.loads((home / "catalog.json").read_text())
dependencies = next(row.get("dependencies", []) for row in catalog["plugins"] if row["name"] == identity.split("@")[0])
missing = [name for name in dependencies if name + "@tj-agents" not in entries]
if missing and not os.environ.get("FIXTURE_IGNORE_DEPENDENCIES"):
    raise SystemExit("missing dependencies before " + operation + ": " + ", ".join(missing))
with (home / "operations.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps([operation, identity]) + "\n")
entries[identity] = [{"scope": scope, "projectPath": str(Path.cwd()), "enabled": True, "version": "published"}]
if os.environ.get("FIXTURE_DROP_INSTALL") != identity:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
if operation == "update":
    print(json.dumps({"updateOutcome": "updated", "newVersion": "published"}))
