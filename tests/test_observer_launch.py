import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/agent/.agents/agent/utility/peer/scripts"
sys.path.insert(0, str(SCRIPTS))
session_close = importlib.import_module("session_close")


def load_reaper() -> Any:
    spec = importlib.util.spec_from_file_location(
        "pending_cleanup_reaper", SCRIPTS / "finish_reaper.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scheduler(arguments: list[str], **kwargs: object) -> SimpleNamespace:
    output = (
        '"EXAMPLE\\user","S-1-5-21-123-456-789-1001"\r\n'
        if Path(arguments[0]).name == "whoami.exe"
        else ""
    )
    return SimpleNamespace(returncode=0, stdout=output, stderr="")


def test_windows_task_starts_the_hidden_launcher_with_exact_arguments(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "installed cli"
    directory.mkdir()
    launcher = directory / "tj_hook.exe"
    launcher.touch()
    launcher.chmod(0o700)
    child = [
        sys.executable,
        "-I",
        "-B",
        "C:/agent scripts/watcher.py",
        "--request",
        'quoted "value"',
    ]
    with mock.patch.object(
        session_close.os, "get_exec_path", return_value=[str(directory)]
    ):
        command = session_close.windows_observer_command(child)
    document = ET.fromstring(
        session_close.windows_task_xml("S-1-5-21-1", command, tmp_path)
    )
    namespace = {"task": session_close.TASK_SCHEDULER_NAMESPACE}
    assert str(launcher.absolute()) == document.findtext(
        ".//task:Command", namespaces=namespace
    )
    assert subprocess.list2cmdline(["--", *child]) == document.findtext(
        ".//task:Arguments", namespaces=namespace
    )


def test_missing_hidden_launcher_refuses_before_scheduling(tmp_path: Path) -> None:
    with (
        mock.patch.object(
            session_close.os, "get_exec_path", return_value=[str(tmp_path)]
        ),
        pytest.raises(session_close.Refusal, match=r"tj_hook\.exe is not installed"),
    ):
        session_close.windows_observer_command([sys.executable, "watcher.py"])


def test_scheduler_controls_use_the_windows_system_directory(tmp_path: Path) -> None:
    system = tmp_path / "Windows/System32"
    system.mkdir(parents=True)
    command = system / "whoami.exe"
    command.touch()
    with (
        mock.patch.dict(
            os.environ, {"SystemRoot": str(system.parent), "PATH": str(tmp_path)}
        ),
        mock.patch("tj_cli.process.run", side_effect=scheduler) as run,
    ):
        assert session_close.current_windows_user({}, 1) == "S-1-5-21-123-456-789-1001"
    assert str(command) == run.call_args.args[0][0]


def test_host_exit_preserves_pending_cleanup_obligation(tmp_path: Path) -> None:
    reaper = load_reaper()
    obligation = tmp_path / "merge-cleanup/obligations/pending.json"
    obligation.parent.mkdir(parents=True)
    content = {
        "worktree": str(tmp_path),
        "session_id": "session",
        "cleanup_status": "pending",
    }
    obligation.write_text(json.dumps(content), encoding="utf-8")
    output = tmp_path / "exited.json"
    arguments = SimpleNamespace(
        state_directory=str(tmp_path),
        host_names=None,
        host_pid=123,
        host_start=1.0,
        worktree=str(tmp_path),
        result=str(output),
        session_id="session",
        invocation_id="invocation",
        parent_pid=None,
        parent_start=None,
        accept_timeout=1.0,
    )
    with (
        mock.patch.object(reaper, "arguments", return_value=arguments),
        mock.patch.object(
            reaper.session_close,
            "process_info",
            return_value=SimpleNamespace(pid=456, started_at=2.0),
        ),
        mock.patch.object(reaper, "await_acceptance", return_value=True),
        mock.patch.object(reaper, "await_exit", return_value=True),
        mock.patch.object(reaper.session_close, "delete_own_session"),
        mock.patch.dict(os.environ, {"AGENT_STATE_DIRECTORY": str(tmp_path)}),
    ):
        assert reaper.main() == 0
    assert content == json.loads(obligation.read_text(encoding="utf-8"))
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "session-closed"
