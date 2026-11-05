"""Load an entry point using only its shipped, explicitly located dependencies."""

import importlib
import importlib.util
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

target = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(target.parent))
if target.name == "checkout.py":
    spec = importlib.util.spec_from_file_location("isolated_checkout", target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
else:
    namespace = runpy.run_path(str(target))
    if target.name == "register_session.py":
        namespace["shared_process"]()
    elif target.name == "session_close.py":
        state = Path(sys.argv[2]).resolve()

        def scheduler(
            arguments: list[str], **options: object
        ) -> subprocess.CompletedProcess[str]:
            output = (
                '"EXAMPLE\\user","S-1-5-21-123-456-789-1001"\n'
                if Path(arguments[0]).name == "whoami.exe"
                else ""
            )
            return subprocess.CompletedProcess(arguments, 0, output, "")

        with (
            mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(state)}),
            mock.patch.object(namespace["process"], "run", side_effect=scheduler),
            mock.patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=1)),
        ):
            namespace["start_observer"](["--result", "unused"])
        staging = next((state / "merge-cleanup/observers").iterdir())
        assert (staging / "tj_cli/process/__init__.py").is_file()
        print(json.dumps({"staging": str(staging)}))
        raise SystemExit(0)

process = importlib.import_module("tj_cli.process")
assert process.__file__ is not None

print(json.dumps({"runtime": str(Path(process.__file__).resolve())}))
