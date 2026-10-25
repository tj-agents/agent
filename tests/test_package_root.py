import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / ".agents" / "agent" / "runtime" / "package_root.py"
spec = importlib.util.spec_from_file_location("agent_package_root", PATH)
PACKAGE_ROOT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PACKAGE_ROOT)


class PackageRootTests(unittest.TestCase):
    def test_receipted_core_stage_is_an_explicit_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".agents" / "agent" / "runtime").mkdir(parents=True)
            (root / "source.json").write_text(json.dumps({
                "repository": "tj-agents/agent", "revision": "abc", "exports": [".agents/agent/runtime"],
                "sha256": "a" * 64,
            }), encoding="utf-8")
            self.assertTrue(PACKAGE_ROOT.is_package_root(root))

    def test_unreceipted_or_wrong_stage_is_not_a_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".agents" / "agent" / "runtime").mkdir(parents=True)
            (root / "source.json").write_text(json.dumps({
                "repository": "tj-agents/other", "revision": "abc", "exports": ["x"], "sha256": "a" * 64,
            }), encoding="utf-8")
            self.assertFalse(PACKAGE_ROOT.is_package_root(root))


if __name__ == "__main__":
    unittest.main()
