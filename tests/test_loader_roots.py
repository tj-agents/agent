import importlib.util
import shutil
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lane_loader_refuses_missing_inner_resolver(loader_package_roots):
    outer, inner = loader_package_roots
    runtime = inner / ".agents/agent/runtime"
    runtime.mkdir(parents=True)
    for name in (
        "agent_cli.py",
        "package_root.py",
        "tj_agent/__init__.py",
        "tj_agent/host.py",
        "launch_errors.py",
        "launch_contract.py",
    ):
        (runtime / name).parent.mkdir(exist_ok=True)
        shutil.copy2(
            ROOT / "plugins/agent/.agents/agent/runtime" / name, runtime / name
        )
    sentinel = outer / ".agents/agent/resources/lanes/resolve.py"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text(
        "raise AssertionError('outer resolver loaded')\n", encoding="utf-8"
    )
    cli = load("nested_agent_cli", runtime / "agent_cli.py")
    with pytest.raises(cli.LaunchError, match="not found under"):
        cli._lanes_module()
