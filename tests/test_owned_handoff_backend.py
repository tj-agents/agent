import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tj_cli import processes

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plugins/agent/.agents/agent/runtime/codex/backend.py"
NATIVE = ROOT / "tests/fixtures/owned_native.py"
EXECUTABLE = str(Path(vars(sys)["_base_executable"]).resolve())
sys.path.insert(0, str(SOURCE.parents[1]))


def load_backend() -> Any:
    spec = importlib.util.spec_from_file_location("owned_backend_subject", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_for(predicate: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError("Owned process fixture did not settle")
        time.sleep(0.02)


def read_record(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def wait_for_process_exit(identity: dict[str, Any]) -> None:
    wait_for(lambda: not processes.matches(identity))


def fixture_main() -> int:
    if sys.argv[1] == "--idle-fixture":
        sys.stdin.buffer.read()
        return 0
    directory = Path(sys.argv[2])
    backend = load_backend()
    lease = backend.Lease(
        EXECUTABLE,
        directory,
        dict(os.environ),
        directory,
        [str(NATIVE)],
    )
    backend.write_record(
        directory / "wrapper.json",
        {
            "backend": lease.backend,
            "supervisor": lease.supervisor,
            "wrapper": backend.identity(os.getpid()),
        },
    )
    if sys.stdin.buffer.read() == b"abort":
        os._exit(71)
    lease.close()
    return 0


@pytest.fixture
def wrappers(
    tmp_path: Path,
) -> Iterator[Callable[[str], tuple[subprocess.Popen[bytes], Path]]]:
    running: list[subprocess.Popen[bytes]] = []
    directories: list[Path] = []

    def start(name: str) -> tuple[subprocess.Popen[bytes], Path]:
        directory = tmp_path / (name + " launch café")
        directory.mkdir()
        temporary = directory / "socket space café"
        temporary.mkdir()
        environment = {
            **os.environ,
            "OWNED_FIXTURE_DIRECTORY": str(directory),
            "TMP": str(temporary),
            "TEMP": str(temporary),
        }
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--wrapper-fixture",
                str(directory),
            ],
            cwd=directory,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        running.append(process)
        directories.append(directory)
        wait_for(
            lambda: (directory / "wrapper.json").exists() or process.poll() is not None,
            timeout=20,
        )
        assert (directory / "wrapper.json").exists(), read_record(
            directory / "backend.json"
        )
        wait_for(lambda: (directory / "native.json").exists())
        return process, directory

    yield start
    for process in running:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=15)
        if process.stdin is not None:
            process.stdin.close()
    for directory in directories:
        receipt = directory / "wrapper.json"
        if receipt.exists():
            identity = read_record(receipt)["supervisor"]
            wait_for_process_exit(identity)


@pytest.mark.parametrize(
    "abrupt", [False, True], ids=["pipe-eof", "wrapper-terminated"]
)
def test_wrapper_exit_reaps_its_backend_and_private_endpoint(
    wrappers: Callable[[str], tuple[subprocess.Popen[bytes], Path]], abrupt: bool
) -> None:
    wrapper, directory = wrappers("single")
    binding = read_record(directory / "wrapper.json")
    native = read_record(directory / "native.json")
    socket = Path(binding["backend"]["socket_path"])
    assert processes.matches(binding["backend"])
    assert binding["backend"]["pid"] == native["pid"]
    info = processes.process_info(native["pid"])
    assert info is not None
    assert binding["backend"]["started_at"] == info.started_at
    assert processes.descends_from(binding["backend"], binding["supervisor"])
    assert processes.descends_from(binding["supervisor"], binding["wrapper"])
    assert native["cwd"] == str(directory)
    assert socket.is_relative_to(directory / "socket space café")
    assert wrapper.stdin is not None
    if abrupt:
        wrapper.stdin.write(b"abort")
    wrapper.stdin.close()
    wrapper.wait(timeout=15)
    wait_for(lambda: not processes.matches(binding["backend"]))
    wait_for(lambda: not processes.matches(binding["supervisor"]))
    assert processes.process_info(native["pid"]) is None
    receipt = read_record(directory / "backend.json")
    assert receipt["state"] == "exited"
    assert receipt["token"] == directory.name
    assert receipt["wrapper"] == binding["wrapper"]
    assert not socket.exists()
    assert not socket.parent.exists()
    if not abrupt:
        assert wrapper.returncode == 0


def test_one_wrapper_exit_preserves_the_other_owned_backend(
    wrappers: Callable[[str], tuple[subprocess.Popen[bytes], Path]],
) -> None:
    first, first_directory = wrappers("first")
    second, second_directory = wrappers("second")
    first_binding = read_record(first_directory / "wrapper.json")
    second_binding = read_record(second_directory / "wrapper.json")
    assert (
        first_binding["backend"]["pid"]
        == read_record(first_directory / "native.json")["pid"]
    )
    assert (
        second_binding["backend"]["pid"]
        == read_record(second_directory / "native.json")["pid"]
    )
    assert first.stdin is not None
    first.stdin.write(b"abort")
    first.stdin.close()
    first.wait(timeout=15)
    wait_for(lambda: not processes.matches(first_binding["supervisor"]))
    assert read_record(first_directory / "backend.json")["state"] == "exited"
    assert not processes.matches(first_binding["backend"])
    assert processes.matches(second_binding["backend"])
    assert processes.matches(second_binding["supervisor"])
    assert second.poll() is None
    assert Path(second_binding["backend"]["socket_path"]).exists()


def test_startup_identity_failure_reaps_the_child_and_removes_its_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = load_backend()
    directory = tmp_path / "failed launch café"
    directory.mkdir()
    socket = directory / "private socket" / "s"
    monkeypatch.setenv("OWNED_FIXTURE_DIRECTORY", str(directory))
    real_identity = backend.identity
    wrapper = real_identity(os.getppid())

    def reject_identity(pid: int) -> dict[str, int | float]:
        if pid == os.getpid():
            return dict(real_identity(pid))
        wait_for(lambda: (directory / "native.json").exists())
        raise RuntimeError("Fixture process identity unavailable")

    monkeypatch.setattr(backend, "identity", reject_identity)
    result = backend.supervise(
        {
            "directory": str(directory),
            "project": str(directory),
            "socket_path": str(socket),
            "executable": EXECUTABLE,
            "arguments": [str(NATIVE)],
            "token": directory.name,
            "wrapper": wrapper,
        },
        io.BytesIO(),
    )
    assert result == 1
    assert read_record(directory / "backend.json")["state"] == "failed"
    assert processes.process_info(read_record(directory / "native.json")["pid"]) is None
    assert not socket.exists()
    assert not socket.parent.exists()


@pytest.mark.parametrize("replaced", ["socket", "parent"])
def test_wrapper_cleanup_preserves_replaced_endpoint_objects(
    wrappers: Callable[[str], tuple[subprocess.Popen[bytes], Path]], replaced: str
) -> None:
    wrapper, directory = wrappers("replacement")
    binding = read_record(directory / "wrapper.json")
    socket = Path(binding["backend"]["socket_path"])
    captured = read_record(directory / "backend.json")["filesystem"]
    if replaced == "socket":
        socket.rename(socket.with_name("original"))
    else:
        socket.parent.rename(socket.parent.with_name("original-parent"))
        socket.parent.mkdir()
    socket.write_bytes(b"replacement endpoint")
    assert wrapper.stdin is not None
    wrapper.stdin.close()
    wrapper.wait(timeout=15)
    wait_for_process_exit(binding["supervisor"])
    assert not processes.matches(binding["backend"])
    assert wrapper.returncode != 0
    receipt = read_record(directory / "backend.json")
    assert receipt["state"] == "failed"
    assert receipt["filesystem"] == captured
    assert "cannot verify" in receipt["cleanup_error"]
    assert socket.read_bytes() == b"replacement endpoint"
    assert socket.parent.is_dir()


def test_startup_cleanup_preserves_never_captured_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = load_backend()
    socket = tmp_path / "unobserved socket" / "s"
    monkeypatch.setenv("OWNED_FIXTURE_DIRECTORY", str(tmp_path))

    def fail_before_capture(_endpoint: Any, _child: subprocess.Popen[bytes]) -> None:
        wait_for(lambda: (tmp_path / "native.json").exists())
        raise RuntimeError("Fixture endpoint capture unavailable")

    monkeypatch.setattr(backend.Endpoint, "capture", fail_before_capture)
    result = backend.supervise(
        {
            "directory": str(tmp_path),
            "project": str(tmp_path),
            "socket_path": str(socket),
            "executable": EXECUTABLE,
            "arguments": [str(NATIVE)],
            "token": tmp_path.name,
            "wrapper": backend.identity(os.getppid()),
        },
        io.BytesIO(),
    )
    assert result == 1
    wait_for(
        lambda: (
            processes.process_info(read_record(tmp_path / "native.json")["pid"]) is None
        )
    )
    receipt = read_record(tmp_path / "backend.json")
    assert receipt["state"] == "failed"
    assert receipt["filesystem"] == {"parent": None, "socket": None}
    assert "cannot verify" in receipt["cleanup_error"]
    assert socket.read_bytes() == b"owned fixture"


def test_cleanup_preserves_parent_when_original_socket_was_never_observed(
    tmp_path: Path,
) -> None:
    backend = load_backend()
    socket = tmp_path / "incomplete endpoint" / "s"
    socket.parent.mkdir()
    endpoint = backend.Endpoint(socket)
    endpoint.parent_identity = endpoint.identity(socket.parent)

    with pytest.raises(RuntimeError, match="never captured its original socket"):
        endpoint.remove()

    assert socket.parent.is_dir()
    assert endpoint.identity(socket.parent) == endpoint.parent_identity


@pytest.mark.parametrize(
    "selection",
    [
        "root",
        "child",
        "foreign",
        "executable",
        "endpoint",
        "duplicate",
        "reused",
        "missing",
    ],
)
def test_native_identity_requires_one_live_exact_command_in_the_launch_tree(
    monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    from codex import client
    from startup_binding import register_session

    backend = load_backend()
    command = [EXECUTABLE, "app-server", "--listen", "unix:///private/s"]
    infos = {
        201: processes.ProcessInfo(201, 1, 201.0),
        202: processes.ProcessInfo(202, 201, 202.0),
        203: processes.ProcessInfo(203, 201, 203.0),
    }
    commands = {201: ["launcher"], 202: command, 203: ["other"]}
    if selection == "root":
        commands[201], commands[202] = command, ["other"]
    elif selection == "foreign":
        infos[202] = infos[202]._replace(ppid=999)
    elif selection == "executable":
        commands[202] = [EXECUTABLE + ".foreign", *command[1:]]
    elif selection == "endpoint":
        commands[202] = [*command[:-1], "unix:///foreign/s"]
    elif selection == "duplicate":
        commands[203] = command
    elif selection == "missing":
        commands[202] = ["other"]

    def arguments(pid: int) -> list[str]:
        if selection == "reused" and pid == 202:
            infos[202] = infos[202]._replace(started_at=999.0)
        return commands[pid]

    monkeypatch.setattr(processes, "process_info", infos.get)
    monkeypatch.setattr(register_session, "process_table", lambda: infos)
    monkeypatch.setattr(client, "process_arguments", arguments)
    clock = iter([0.0, 3.0])
    monkeypatch.setattr(backend.time, "monotonic", lambda: next(clock))
    child = SimpleNamespace(pid=201, poll=lambda: None)
    if selection in {"root", "child"}:
        pid = 201 if selection == "root" else 202
        assert backend.native_identity(child, command) == {
            "pid": pid,
            "started_at": float(pid),
        }
    else:
        with pytest.raises(RuntimeError, match=r"identity|ambiguous"):
            backend.native_identity(child, command)


@pytest.mark.parametrize("changed", ["token", "wrapper"])
def test_supervisor_refuses_a_foreign_launch_before_starting_native_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    backend = load_backend()
    monkeypatch.setenv("OWNED_FIXTURE_DIRECTORY", str(tmp_path))
    socket = tmp_path / "private socket" / "s"
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    with subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--idle-fixture"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    ) as foreign:
        specification = {
            "directory": str(tmp_path),
            "project": str(tmp_path),
            "socket_path": str(socket),
            "executable": EXECUTABLE,
            "arguments": [str(NATIVE)],
            "token": "other" if changed == "token" else tmp_path.name,
            "wrapper": backend.identity(
                foreign.pid if changed == "wrapper" else os.getppid()
            ),
        }
        try:
            assert backend.supervise(specification, io.BytesIO()) == 1
        finally:
            assert foreign.stdin is not None
            foreign.stdin.close()
            foreign.wait(timeout=10)
    assert "does not belong" in read_record(tmp_path / "backend.json")["error"]
    assert not (tmp_path / "native.json").exists()
    assert not socket.parent.exists()


if __name__ == "__main__":
    raise SystemExit(fixture_main())
