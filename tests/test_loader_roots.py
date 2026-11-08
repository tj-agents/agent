import importlib.util
import shutil
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def followup(root):
    script = root / ".agents/agent/utility/message/scripts/codex_followup.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(
        ROOT / "plugins/agent/.agents/agent/utility/message/scripts/codex_followup.py",
        script,
    )
    return load("nested_codex_followup_" + root.name, script)


def client_runtime(followup):
    modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "codex" or name.startswith("codex.")
    }
    paths = list(sys.path)
    for name in modules:
        sys.modules.pop(name)
    try:
        return followup.client_runtime()
    finally:
        for name in tuple(sys.modules):
            if name == "codex" or name.startswith("codex."):
                sys.modules.pop(name)
        sys.modules.update(modules)
        sys.path[:] = paths


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


def test_store_loader_refuses_missing_inner_store(loader_package_roots):
    outer, inner = loader_package_roots
    scripts = inner / ".agents/agent/utility/peer/scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(
        ROOT / "plugins/agent/.agents/agent/utility/peer/scripts/register_session.py",
        scripts / "register_session.py",
    )
    sentinel = outer / ".agents/agent-store/runtime/session_store.py"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text(
        "raise AssertionError('outer store loaded')\n", encoding="utf-8"
    )
    register = load("nested_register_session", scripts / "register_session.py")
    assert register._load_session_store() is None


def test_followup_loader_refuses_missing_inner_transport(loader_package_roots):
    outer, inner = loader_package_roots
    module = followup(inner)
    sentinel = outer / ".agents/agent/runtime/codex/hook_trust.py"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text(
        "raise AssertionError('outer transport loaded')\n", encoding="utf-8"
    )
    with pytest.raises(module.FollowupError, match="transport was not found"):
        module.Proxy("codex", inner, time.monotonic() + 1)


def test_followup_client_refuses_outer_runtime_after_an_inner_marker(
    loader_package_roots,
):
    outer, inner = loader_package_roots
    module = followup(inner)
    sentinel = outer / ".agents/agent/runtime/codex/client.py"
    sentinel.parent.mkdir(parents=True)
    sentinel.with_name("__init__.py").write_text("", encoding="utf-8")
    sentinel.write_text(
        "raise AssertionError('outer client loaded')\n", encoding="utf-8"
    )
    with pytest.raises(module.FollowupError, match="client is unavailable"):
        client_runtime(module)


def test_followup_client_accepts_the_nearest_packaged_runtime(loader_package_roots):
    _, inner = loader_package_roots
    module = followup(inner)
    client = inner / ".agents/agent/runtime/codex/client.py"
    client.parent.mkdir(parents=True)
    client.with_name("__init__.py").write_text("", encoding="utf-8")
    client.write_text("SOURCE = 'inner'\n", encoding="utf-8")
    runtime = client_runtime(module)
    assert runtime.SOURCE == "inner"
    assert Path(runtime.__file__).resolve() == client.resolve()


def test_followup_client_refuses_when_no_package_marker_exists(tmp_path):
    orphan = tmp_path / "orphan"
    module = followup(orphan)
    with pytest.raises(module.FollowupError, match="client is unavailable"):
        client_runtime(module)


def test_followup_client_refuses_a_cached_outer_origin(
    loader_package_roots, monkeypatch
):
    outer, inner = loader_package_roots
    module = followup(inner)
    client = inner / ".agents/agent/runtime/codex/client.py"
    client.parent.mkdir(parents=True)
    client.with_name("__init__.py").write_text("", encoding="utf-8")
    client.write_text("SOURCE = 'inner'\n", encoding="utf-8")
    cached = types.ModuleType("codex.client")
    cached.__file__ = str(outer / ".agents/agent/runtime/codex/client.py")
    package = types.ModuleType("codex")
    package.client = cached
    monkeypatch.setitem(sys.modules, "codex", package)
    monkeypatch.setitem(sys.modules, "codex.client", cached)
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(module.FollowupError, match="did not load"):
        module.client_runtime()
