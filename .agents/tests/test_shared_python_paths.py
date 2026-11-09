import shutil
import subprocess
import sys
from pathlib import Path


def test_packaged_startup_imports_its_shipped_library_in_isolated_python(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    installed = tmp_path / "installed"
    shutil.copytree(root / "plugins/agent", installed)

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(installed / ".agents/agent/runtime/session_hook.py"),
            "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )

    assert "usage:" in result.stdout
    assert (installed / ".agents/agent/runtime/tj_cli/paths/search.py").is_file()
