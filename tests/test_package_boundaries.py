import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from package_test_support import bundle

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / ".agents/agent/skills/peer/scripts/register_session.py"


def native_package(root: Path) -> Path:
    declaration = root / ".codex-plugin/plugin.json"
    declaration.parent.mkdir(parents=True)
    declaration.write_text(json.dumps({"name": "agent"}), encoding="utf-8")
    target = root / "hooks/register_session.py"
    target.parent.mkdir(parents=True)
    shutil.copy2(ADAPTER, target)
    return target


class PackageBoundaryTests(unittest.TestCase):
    def test_missing_nearest_bundle_refuses_outer_and_installed_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            outer = Path(temporary) / "outer"
            native_package(outer)
            package = outer / "python/tj_agent"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text(
                "raise AssertionError('outer package imported')\n", encoding="utf-8"
            )
            script = native_package(outer / "plugins/agent/local")
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(script)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn(
                "declared tj_agent import boundary is unavailable", result.stderr
            )
            self.assertNotIn("outer package imported", result.stderr)

    def test_missing_dependency_refuses_installed_packages(self) -> None:
        for missing in ("tj_agent_store", "tj_cli"):
            with (
                self.subTest(missing=missing),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary) / "plugin"
                script = native_package(root)
                for name in {"tj_agent", "tj_agent_store", "tj_cli"} - {missing}:
                    package = root / "python" / name
                    package.mkdir(parents=True)
                    (package / "__init__.py").write_text("", encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, "-I", "-B", str(script)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(0, result.returncode)
                self.assertIn(
                    f"declared {missing} import boundary is unavailable", result.stderr
                )

    def test_hook_acknowledgment_uses_the_selected_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = bundle(Path(temporary) / "plugin")
            declaration = root / ".codex-plugin/plugin.json"
            declaration.parent.mkdir()
            declaration.write_text(json.dumps({"name": "agent"}), encoding="utf-8")
            (root / "harness.json").write_text(
                json.dumps({"plugin": "tj-agents/agent"}), encoding="utf-8"
            )
            adapter = root / "hooks/hook_dispatch.py"
            adapter.parent.mkdir()
            shutil.copy2(ROOT / ".agents/hooks/hook_dispatch.py", adapter)
            (adapter.parent / "ready.py").write_text("print('{}')", encoding="utf-8")
            receipts = Path(temporary) / "receipts"
            receipts.mkdir()
            token = "a" * 32
            environment = {
                **os.environ,
                "TJ_AGENTS_STARTUP_ACK_DIRECTORY": str(receipts),
                "TJ_AGENTS_STARTUP_ACK_TOKEN": token,
                "TJ_AGENTS_STARTUP_REQUIRED_HOOKS": json.dumps(["agent@tj-agents"]),
            }
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(adapter), "--hook", "ready.py"],
                input=json.dumps({"hook_event_name": "SessionStart"}),
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(token, (receipts / token).read_text(encoding="utf-8"))

    def test_snapshot_uses_selected_package_after_original_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "cache/agent/local"
            script = native_package(root)
            package = root / "python/tj_agent"
            (package / "sessions").mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "sessions/__init__.py").write_text("", encoding="utf-8")
            for dependency in ("tj_agent_store", "tj_cli"):
                dependency_root = root / "python" / dependency
                dependency_root.mkdir()
                (dependency_root / "__init__.py").write_text("", encoding="utf-8")
            (package / "sessions/register_session.py").write_text(
                "from pathlib import Path\nimport base64\nimport sys\nimport tj_agent\n"
                "Path(base64.b64decode(sys.argv[1]).decode()).write_text("
                "\"raise AssertionError('original package imported')\\n\", "
                "encoding='utf-8')\n"
                "print(Path(tj_agent.__file__).resolve())\n",
                encoding="utf-8",
            )
            snapshot = ROOT / ".codex/hooks/snapshot.py"
            spec = importlib.util.spec_from_file_location("boundary_snapshot", snapshot)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            digest = module.digest_tree(root)
            environment = {
                "TJ_AGENTS_STARTUP_SNAPSHOT_DIRECTORY": str(
                    Path(temporary) / "snapshots"
                )
            }
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(snapshot),
                    str(root),
                    digest,
                    "agent",
                    str(script),
                    base64.b64encode(str(package / "__init__.py").encode()).decode(),
                ],
                env={**os.environ, **environment},
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            origin = Path(result.stdout.strip())
            self.assertTrue(origin.is_file())
            self.assertTrue(
                origin.is_relative_to((Path(temporary) / "snapshots").resolve()),
                result.stdout + result.stderr,
            )
            self.assertIn(
                "original package imported", (package / "__init__.py").read_text()
            )


if __name__ == "__main__":
    unittest.main()
