import subprocess
import sys
from pathlib import Path
from typing import NoReturn

import install
import pytest
from codex import launch, observer


def test_no_socket_needs_no_observer(tmp_path: Path) -> None:
    def unexpected(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("a missing daemon must not start a process")

    assert observer.status(tmp_path, tmp_path, run=unexpected)
    assert not observer.ensure(tmp_path, tmp_path, run=unexpected)


def test_hook_entrypoint_works_without_a_running_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    script = Path(observer.__file__).parent.parent / "observer_start.py"
    result = subprocess.run(
        [sys.executable, "-B", str(script)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert result.stderr == ""


def test_start_waits_for_ready_without_restarting_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = observer.socket_path(tmp_path)
    socket.parent.mkdir()
    socket.touch()
    calls: list[tuple[list[str], dict[str, object]]] = []
    results = iter((1, 0, 0))

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, next(results), "", "")

    def executable(*, match_daemon: bool) -> str:
        assert match_daemon
        return "exact-native"

    monkeypatch.setattr(launch, "executable", executable)
    assert observer.ensure(tmp_path, tmp_path, run=run)
    assert [command[-1] for command, _ in calls] == ["--status", "--start", "--status"]
    assert calls[1][1]["preserve_descendants"] is True
    assert all(str(socket) in command for command, _ in calls)
    assert all(
        command[command.index("--codex") + 1] == "exact-native" for command, _ in calls
    )


def test_idle_daemon_needs_no_observer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = observer.socket_path(tmp_path)
    socket.parent.mkdir()
    socket.touch()

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 3, "", "")

    def executable(*, match_daemon: bool) -> str:
        assert match_daemon
        return "exact-native"

    monkeypatch.setattr(launch, "executable", executable)
    assert observer.status(tmp_path, tmp_path, run=run)


def test_last_thread_exits_during_start_without_failing_setup(tmp_path: Path) -> None:
    socket = observer.socket_path(tmp_path)
    socket.parent.mkdir()
    socket.touch()
    results = iter((1, 3))

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, next(results), "", "")

    assert not observer.ensure(tmp_path, tmp_path, native="exact-native", run=run)


def test_daemon_errors_are_not_reported_as_ready(tmp_path: Path) -> None:
    socket = observer.socket_path(tmp_path)
    socket.parent.mkdir()
    socket.touch()

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, "", "connection failed")

    with pytest.raises(RuntimeError, match="connection failed"):
        observer.status(tmp_path, tmp_path, native="exact-native", run=run)


def test_failed_install_does_not_start_the_observer(tmp_path: Path) -> None:
    def fail(_context: install.Context) -> list[str]:
        raise RuntimeError("plugin update failed")

    def unexpected(_context: install.Context) -> list[str]:
        pytest.fail("observer must not start after a failed install step")

    context = install.Context(home=tmp_path, environ={}, windows=False)
    steps = (
        install.Step("plugin setup", lambda _context: ["repair"], fail),
        install.Step("Codex update observer", lambda _context: ["start"], unexpected),
    )
    assert install.run_steps(steps, context, False) == 2


def test_failed_plugin_update_does_not_start_the_observer(tmp_path: Path) -> None:
    def unexpected(_context: install.Context) -> list[str]:
        pytest.fail("observer must not start after a failed plugin update")

    context = install.Context(
        home=tmp_path,
        environ={},
        windows=False,
        failures=["plugins: convergence failed"],
    )
    steps = (
        install.Step("Codex update observer", lambda _context: ["start"], unexpected),
    )
    install.run_steps(steps, context, False)
    assert context.failures == ["plugins: convergence failed"]
