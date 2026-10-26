import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agents/agent/runtime"))
SPEC = importlib.util.spec_from_file_location("runtime_delivery_tests", ROOT / ".agents/agent/runtime/runtime_delivery.py")
DELIVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DELIVERY)


class RuntimeDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runtime-delivery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "bin"
        self.source = self.root / "source"

    def desired(self, files, revision="source"):
        for name, body in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        exports = [".agents/agent/runtime"]
        contents = DELIVERY.closure(self.source, exports)
        return {"repository": DELIVERY.REPOSITORY, "revision": revision, "exports": exports,
                "sha256": DELIVERY.digest(contents),
                "files": {name: hashlib.sha256(body).hexdigest() for name, body in contents.items()}}

    def test_retired_owned_file_is_removed_but_unknown_extra_survives(self):
        first = self.desired({".agents/agent/runtime/live.py": b"live", ".agents/agent/runtime/retired.py": b"old"}, "v1")
        self.assertEqual([], DELIVERY.apply(self.target, self.source, first))
        (self.source / ".agents/agent/runtime/retired.py").unlink()
        second = self.desired({".agents/agent/runtime/live.py": b"live"}, "v2")
        extra = self.target / ".agents/agent/runtime/extra.py"
        extra.write_bytes(b"mine")
        self.assertEqual([], DELIVERY.apply(self.target, self.source, second))
        self.assertFalse((self.target / ".agents/agent/runtime/retired.py").exists())
        self.assertEqual(b"mine", extra.read_bytes())
        self.assertEqual([], DELIVERY.check(self.target, self.source, second))

    def test_dirty_owned_is_repaired_but_dirty_retired_blocks_convergence(self):
        first = self.desired({".agents/agent/runtime/retired.py": b"old"}, "v1")
        DELIVERY.apply(self.target, self.source, first)
        owned = self.target / ".agents/agent/runtime/retired.py"
        owned.write_bytes(b"changed")
        (self.source / ".agents/agent/runtime/retired.py").unlink()
        second = self.desired({".agents/agent/runtime/live.py": b"new"}, "v2")
        errors = DELIVERY.apply(self.target, self.source, second)
        self.assertTrue(any("retired" in error for error in errors))
        self.assertEqual(b"changed", owned.read_bytes())

    def test_unowned_incoming_collision_is_preserved_and_pending(self):
        self.target.mkdir()
        collision = self.target / ".agents/agent/runtime/live.py"
        collision.parent.mkdir(parents=True)
        collision.write_bytes(b"local")
        desired = self.desired({".agents/agent/runtime/live.py": b"published"})
        errors = DELIVERY.apply(self.target, self.source, desired)
        self.assertTrue(any("unowned" in error for error in errors))
        self.assertEqual(b"local", collision.read_bytes())
        self.assertFalse((self.target / "source.json").exists())
        retry = DELIVERY.apply(self.target, self.source, desired)
        self.assertTrue(any("unowned" in error for error in retry))
        self.assertEqual(b"local", collision.read_bytes())

    def test_state_root_with_a_linked_ancestor_is_refused_without_touching_outside(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "sentinel").write_bytes(b"keep")
        linked = self.root / "state"
        try:
            os.symlink(outside, linked, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"links unavailable: {error}")
        desired = self.desired({".agents/agent/runtime/live.py": b"published"})
        with self.assertRaisesRegex(DELIVERY.DeliveryError, "traverses a link"):
            DELIVERY.apply(linked / "bin", self.source, desired)
        self.assertEqual(b"keep", (outside / "sentinel").read_bytes())
        self.assertFalse((outside / "bin").exists())

    def test_linked_source_root_and_malformed_exports_are_refused(self):
        desired = self.desired({".agents/agent/runtime/live.py": b"published"})
        source_link = self.root / "source-link"
        try:
            os.symlink(self.source, source_link, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"links unavailable: {error}")
        with self.assertRaisesRegex(DELIVERY.DeliveryError, "source root traverses a link"):
            DELIVERY.closure(source_link, desired["exports"])
        malformed = dict(desired)
        malformed["exports"] = [{}]
        with self.assertRaises(DELIVERY.DeliveryError):
            DELIVERY.validate_receipt(malformed, self.source)

    def test_unchanged_apply_is_idempotent(self):
        desired = self.desired({".agents/agent/runtime/live.py": b"published"})
        self.assertEqual([], DELIVERY.apply(self.target, self.source, desired))
        payload = self.target / ".agents/agent/runtime/live.py"
        before = (payload.stat().st_mtime_ns, (self.target / "source.json").stat().st_mtime_ns)
        self.assertEqual([], DELIVERY.apply(self.target, self.source, desired))
        self.assertEqual(before, (payload.stat().st_mtime_ns, (self.target / "source.json").stat().st_mtime_ns))

    def test_interrupted_owned_delivery_resumes_from_the_pending_receipt(self):
        first = self.desired({".agents/agent/runtime/retired.py": b"old"}, "v1")
        self.assertEqual([], DELIVERY.apply(self.target, self.source, first))
        (self.source / ".agents/agent/runtime/retired.py").unlink()
        second = self.desired({".agents/agent/runtime/live.py": b"new"}, "v2")
        (self.target / "source.json").write_text(json.dumps(DELIVERY._pending(first, second)), encoding="utf-8")
        self.assertEqual([], DELIVERY.apply(self.target, self.source, second))
        self.assertFalse((self.target / ".agents/agent/runtime/retired.py").exists())
        self.assertEqual(b"new", (self.target / ".agents/agent/runtime/live.py").read_bytes())
        self.assertEqual([], DELIVERY.check(self.target, self.source, second))


if __name__ == "__main__":
    unittest.main()
