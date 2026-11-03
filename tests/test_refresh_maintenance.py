from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".agents/agent/runtime"
sys.path.insert(0, str(RUNTIME))

plugin_refresh = importlib.import_module("plugin_refresh")
required_plugins = importlib.import_module("required_plugins")
Host = importlib.import_module("host").Host


def load_reload_plugins() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "reload_plugins_maintenance", RUNTIME / "reload_plugins.py"
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader = spec.loader
    assert loader is not None
    loader.exec_module(module)
    return module


def test_deferred_maintenance_verifies_and_never_adopts() -> None:
    reload_plugins = load_reload_plugins()
    plugins = SimpleNamespace(
        resolve_executable=mock.Mock(return_value="native"),
        ensure_hooks=mock.Mock(return_value=None),
        adopt=mock.Mock(),
    )
    output = io.StringIO()
    with (
        mock.patch.dict(reload_plugins.PLUGINS, {Host.CODEX: plugins}),
        mock.patch.object(reload_plugins, "prepare_required"),
        mock.patch.object(
            reload_plugins, "selection", return_value=({}, ["agent@tj-agents"])
        ),
        mock.patch.object(reload_plugins, "refresh", return_value=["agent@tj-agents"]),
        mock.patch.object(
            reload_plugins,
            "verify",
            return_value={"host": "codex", "plugins": [], "problems": []},
        ),
        mock.patch.object(
            reload_plugins.required_plugins, "converge_setup", return_value=[]
        ) as setup,
        contextlib.redirect_stdout(output),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        assert (
            reload_plugins.main(
                [
                    "--host",
                    "codex",
                    "--verify",
                    "--no-relaunch",
                    "--defer-adoption",
                    "--maintenance-budget",
                    "240",
                ]
            )
            == 0
        )
    assert json.loads(output.getvalue())["adoption"] == "deferred"
    plugins.adopt.assert_not_called()
    setup.assert_called_once()


def test_deferred_maintenance_reports_refresh_failure_without_adoption() -> None:
    reload_plugins = load_reload_plugins()
    plugins = SimpleNamespace(
        resolve_executable=mock.Mock(return_value="native"), adopt=mock.Mock()
    )
    output = io.StringIO()
    with (
        mock.patch.dict(reload_plugins.PLUGINS, {Host.CODEX: plugins}),
        mock.patch.object(reload_plugins, "prepare_required"),
        mock.patch.object(
            reload_plugins, "selection", return_value=({}, ["agent@tj-agents"])
        ),
        mock.patch.object(
            reload_plugins,
            "refresh",
            side_effect=reload_plugins.RefreshError("network failed"),
        ),
        mock.patch.object(reload_plugins.required_plugins, "converge_setup") as setup,
        contextlib.redirect_stdout(output),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        assert (
            reload_plugins.main(
                [
                    "--host",
                    "codex",
                    "--verify",
                    "--no-relaunch",
                    "--defer-adoption",
                    "--maintenance-budget",
                    "240",
                ]
            )
            == 1
        )
    result = json.loads(output.getvalue())
    assert result["adoption"] == "deferred"
    assert result["reports"][0]["problems"] == ["network failed"]
    plugins.adopt.assert_not_called()
    setup.assert_not_called()


def test_deferred_maintenance_returns_three_when_writer_settlement_is_unproven() -> (
    None
):
    reload_plugins = load_reload_plugins()
    plugins = SimpleNamespace(
        resolve_executable=mock.Mock(return_value="native"), adopt=mock.Mock()
    )
    output = io.StringIO()
    with (
        mock.patch.dict(reload_plugins.PLUGINS, {Host.CODEX: plugins}),
        mock.patch.object(reload_plugins, "prepare_required"),
        mock.patch.object(
            reload_plugins, "selection", return_value=({}, ["agent@tj-agents"])
        ),
        mock.patch.object(
            reload_plugins,
            "refresh",
            side_effect=plugin_refresh.MaintenanceUnsettledError(
                "Maintenance writer settlement could not be proven"
            ),
        ),
        contextlib.redirect_stdout(output),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        assert (
            reload_plugins.main(
                [
                    "--host",
                    "codex",
                    "--verify",
                    "--no-relaunch",
                    "--defer-adoption",
                    "--maintenance-budget",
                    "180",
                ]
            )
            == 3
        )
    result = json.loads(output.getvalue())
    assert result["reports"][0]["writer_unsettled"] is True
    plugins.adopt.assert_not_called()


def test_maintenance_budget_uses_the_remaining_deadline_for_each_native_command() -> (
    None
):
    observed: list[float] = []
    completed = plugin_refresh.subprocess.CompletedProcess(["native"], 0, "ok", "")

    def run(*args: object, **kwargs: object) -> object:
        timeout = kwargs["timeout"]
        assert isinstance(timeout, float)
        observed.append(timeout)
        return completed

    with mock.patch.object(
        plugin_refresh.process,
        "run",
        side_effect=run,
    ):
        assert (
            plugin_refresh.run(
                ["native", "plugin", "list"], ROOT, deadline=time.monotonic() + 2
            )
            == "ok"
        )
    assert 0 < observed[0] <= 2


def test_expired_maintenance_budget_settles_the_native_writer_tree() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ready = root / "ready"
        writer = root / "writer"
        child = (
            "from pathlib import Path; import time; "
            f"Path({str(ready)!r}).write_text('ready', encoding='utf-8'); "
            "time.sleep(4); "
            f"Path({str(writer)!r}).write_text('late', encoding='utf-8')"
        )
        parent = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(20)"
        )
        with pytest.raises(plugin_refresh.MaintenanceUnsettledError):
            plugin_refresh.run(
                [sys.executable, "-c", parent], root, deadline=time.monotonic() + 3
            )
        time.sleep(4.2)
        assert ready.is_file()
        assert not writer.exists()


def test_maintenance_budget_settles_a_plugin_setup_writer_tree() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ready = root / "ready"
        writer = root / "writer"
        script = root / "setup.py"
        child = (
            "from pathlib import Path; import time; "
            f"Path({str(ready)!r}).write_text('ready'); time.sleep(4); "
            f"Path({str(writer)!r}).write_text('late')"
        )
        script.write_text(
            "import subprocess\n"
            "import sys\n"
            "import time\n"
            f"child = {child!r}\n"
            "subprocess.Popen([sys.executable, '-c', child])\n"
            "time.sleep(20)\n",
            encoding="utf-8",
        )
        target = required_plugins.Target("fixture@tj-agents", script, "setup.py", b"")
        with (
            plugin_refresh.maintenance_budget(3),
            pytest.raises(plugin_refresh.MaintenanceUnsettledError),
        ):
            required_plugins.invoke(target, [], 90)
        time.sleep(4.2)
        assert ready.is_file()
        assert not writer.exists()


def test_maintenance_uses_the_installed_tj_hook_runner_when_available() -> None:
    if shutil.which("tj_hook") is None and shutil.which("tj_hook.exe") is None:
        pytest.skip("tj_hook is unavailable")
    with plugin_refresh.maintenance_budget(5):
        assert plugin_refresh.run([sys.executable, "-c", "print('ok')"], ROOT) == "ok\n"
