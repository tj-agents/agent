import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


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
            str(installed / ".agents/agent/runtime/startup_binding.py"),
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


@pytest.mark.skipif(
    os.name == "nt", reason="managed Python PATH is a POSIX launch boundary"
)
def test_repeated_launches_preserve_literal_path_without_duplicate_prefixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = (
        Path(__file__).resolve().parents[2] / "plugins/agent/.agents/agent/runtime"
    )
    monkeypatch.syspath_prepend(str(runtime))
    spec = importlib.util.spec_from_file_location(
        "shared_paths_startup", runtime / "startup_binding.py"
    )
    assert spec is not None and spec.loader is not None
    binding = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(binding)
    interpreter = tmp_path / "bin/python"
    interpreter.parent.mkdir()
    interpreter.touch()
    initial = os.pathsep.join([str(interpreter.parent), "", "/tools", "/tools"])
    monkeypatch.setenv("PATH", initial)
    monkeypatch.setattr(binding.launch_check, "state_directory", lambda: tmp_path)
    process = Mock(pid=72)
    process.wait.return_value = 0
    spawn = Mock(return_value=process)
    monkeypatch.setattr(binding.subprocess, "Popen", spawn)

    assert binding.run_session(binding.Host.CLAUDE, "native", tmp_path, ()) == 0

    assert spawn.call_args.kwargs["env"]["PATH"] == os.pathsep.join(
        [str(interpreter.parent), "", "/tools"]
    )
