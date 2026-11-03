import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from tj_cli import processes

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".codex/runtime/backend.py"


def load_backend() -> Any:
    spec = importlib.util.spec_from_file_location("owned_backend_subject", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_for(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
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
    if sys.argv[1] == "--native-fixture":
        socket = Path(sys.argv[sys.argv.index("--listen") + 1].removeprefix("unix://"))
        socket.parent.mkdir()
        socket.write_bytes(b"owned fixture")
        info = processes.process_info(os.getpid())
        assert info is not None
        load_backend().write_record(
            Path(os.environ["OWNED_FIXTURE_DIRECTORY"]) / "native.json",
            {"pid": info.pid, "started_at": info.started_at, "cwd": str(Path.cwd())},
        )
        while True:
            time.sleep(0.1)
    directory = Path(sys.argv[2])
    backend = load_backend()
    lease = backend.Lease(
        sys.executable,
        directory,
        dict(os.environ),
        directory,
        [str(Path(__file__).resolve()), "--native-fixture"],
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
            lambda: (directory / "wrapper.json").exists() or process.poll() is not None
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
    assert native["cwd"] == str(directory)
    assert socket.is_relative_to(directory / "socket space café")
    assert wrapper.stdin is not None
    if abrupt:
        wrapper.stdin.write(b"abort")
    wrapper.stdin.close()
    wrapper.wait(timeout=15)
    wait_for(lambda: not processes.matches(binding["backend"]))
    wait_for(lambda: not processes.matches(binding["supervisor"]))
    assert not processes.matches(native)
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
            "executable": sys.executable,
            "arguments": [str(Path(__file__).resolve()), "--native-fixture"],
            "token": directory.name,
            "wrapper": wrapper,
        },
        io.BytesIO(),
    )
    assert result == 1
    assert read_record(directory / "backend.json")["state"] == "failed"
    assert not processes.matches(read_record(directory / "native.json"))
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
            "executable": sys.executable,
            "arguments": [str(Path(__file__).resolve()), "--native-fixture"],
            "token": tmp_path.name,
            "wrapper": backend.identity(os.getppid()),
        },
        io.BytesIO(),
    )
    assert result == 1
    wait_for_process_exit(read_record(tmp_path / "native.json"))
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
            "executable": sys.executable,
            "arguments": [str(Path(__file__).resolve()), "--native-fixture"],
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
