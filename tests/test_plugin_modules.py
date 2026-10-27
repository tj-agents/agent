import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins/agent/.agents/agent/runtime"


class PluginModuleTests(unittest.TestCase):
    def test_shipped_sources_work_without_loading_commands_or_host_adapters(self):
        with tempfile.TemporaryDirectory(prefix="isolated Agent library ") as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            shutil.copytree(RUNTIME, runtime)
            package = root / "package"
            package.mkdir()
            (package / "skill.txt").write_bytes(b"published")
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", """
import importlib.abc
from pathlib import Path
import sys

class RejectEntryPoints(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"reload_plugins", "host_plugins", "claude.plugins", "codex.plugins"}:
            raise ImportError("Library imported an entry point: " + fullname)

sys.meta_path.insert(0, RejectEntryPoints())
runtime, root = map(Path, sys.argv[1:])
sys.path.insert(0, str(runtime))
from tj_agent import content
from tj_agent.plugins import sources

files = sources.tree("./package", root, root, root / "temporary", {})
assert files == {"skill.txt": b"published"}
assert len(content.digest(files)) == 64
for module in (content, sources):
    assert Path(module.__file__).resolve().is_relative_to(runtime.resolve())
""", str(runtime), str(root)],
                cwd=root, env={**os.environ, "PYTHONPATH": ""}, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(0, result.returncode, result.stderr)
