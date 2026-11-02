"""Own one native handoff backend through the launch wrapper's pipe lease."""

from __future__ import annotations

import json
import os
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import IO, Any


def identity(pid: int) -> dict[str, int | float]:
    from tj_cli import processes

    info = processes.process_info(pid)
    if info is None:
        raise RuntimeError("Owned launch process identity is unavailable")
    return {"pid": info.pid, "started_at": info.started_at}


def write_record(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def stop(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def remove_socket(socket: Path) -> None:
    socket.unlink(missing_ok=True)
    if socket.parent.exists():
        socket.parent.rmdir()


def read_reply(stream: IO[bytes], result: queue.Queue[bytes]) -> None:
    result.put(stream.readline(65537))


class Lease:
    def __init__(
        self,
        executable: str,
        project: Path,
        environment: dict[str, str],
        directory: Path,
        launch_arguments: list[str],
    ) -> None:
        socket = (
            Path(tempfile.gettempdir()).resolve()
            / ("tja-" + secrets.token_hex(12))
            / "s"
        )
        wrapper = identity(os.getpid())
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve())],
            cwd=project,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.writer = self.process.stdin
        self.reader = self.process.stdout
        try:
            specification = {
                "executable": executable,
                "project": str(project),
                "socket_path": str(socket),
                "directory": str(directory),
                "arguments": launch_arguments,
                "token": directory.name,
                "wrapper": wrapper,
            }
            self.writer.write((json.dumps(specification) + "\n").encode("utf-8"))
            self.writer.flush()
            replies: queue.Queue[bytes] = queue.Queue(maxsize=1)
            threading.Thread(
                target=read_reply, args=(self.reader, replies), daemon=True
            ).start()
            try:
                reply = replies.get(timeout=15)
            except queue.Empty as error:
                raise RuntimeError("Owned backend supervisor did not start") from error
            value = json.loads(reply)
            if value.get("state") != "running":
                raise RuntimeError(value.get("error") or "Owned backend did not start")
            self.backend = value["backend"]
            self.supervisor = value["supervisor"]
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self.writer.close()
        try:
            self.process.wait(timeout=12)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                "Owned backend cleanup is unconfirmed; preserve its launch evidence"
            ) from error
        finally:
            self.reader.close()
        if self.process.returncode:
            raise RuntimeError(
                "Owned backend cleanup failed; preserve its launch evidence"
            )


def wait_for_eof(stream: IO[bytes], ended: threading.Event) -> None:
    try:
        while stream.read(4096):
            pass
    finally:
        ended.set()


def supervise(specification: dict[str, Any], stream: IO[bytes]) -> int:
    from tj_cli import processes

    directory = Path(specification["directory"])
    socket = Path(specification["socket_path"])
    receipt = directory / "backend.json"
    state: dict[str, Any] = {
        "state": "starting",
        "socket_path": str(socket),
        "token": specification["token"],
        "wrapper": specification["wrapper"],
    }
    child = None
    try:
        if not socket.is_absolute() or socket.parent.exists():
            raise RuntimeError(
                "Owned native socket requires a new absolute private directory"
            )
        processes.require_live(
            specification["wrapper"], "Launch wrapper is unavailable"
        )
        if (
            not processes.descends_from(identity(os.getpid()), specification["wrapper"])
            or specification["token"] != directory.name
        ):
            raise RuntimeError(
                "Backend supervisor does not belong to this launch wrapper"
            )
        if (
            not directory.is_absolute()
            or not directory.is_dir()
            or not Path(specification["executable"]).is_absolute()
            or not Path(specification["project"]).is_absolute()
            or not isinstance(specification["arguments"], list)
            or not all(isinstance(value, str) for value in specification["arguments"])
        ):
            raise RuntimeError("Owned native backend specification is invalid")
        with (directory / "backend.log").open("ab") as diagnostic:
            child = subprocess.Popen(
                [
                    specification["executable"],
                    *specification["arguments"],
                    "app-server",
                    "--listen",
                    "unix://" + str(socket),
                ],
                cwd=specification["project"],
                stdin=subprocess.DEVNULL,
                stdout=diagnostic,
                stderr=diagnostic,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                close_fds=True,
            )
            state.update(
                state="running",
                supervisor=identity(os.getpid()),
                backend={
                    "kind": "handoff",
                    **identity(child.pid),
                    "socket_path": str(socket),
                },
            )
            write_record(receipt, state)
            print(json.dumps(state), flush=True)
            ended = threading.Event()
            threading.Thread(
                target=wait_for_eof, args=(stream, ended), daemon=True
            ).start()
            while not ended.wait(0.1):
                if child.poll() is not None:
                    break
            stop(child)
        remove_socket(socket)
        state.update(state="exited", returncode=child.returncode)
        write_record(receipt, state)
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        if child is not None:
            try:
                stop(child)
                remove_socket(socket)
            except (OSError, subprocess.SubprocessError) as cleanup:
                state["cleanup_error"] = str(cleanup)
        state.update(state="failed", error=str(error))
        write_record(receipt, state)
        print(json.dumps(state), flush=True)
        return 1


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raw = sys.stdin.buffer.readline(65537)
    if not raw or len(raw) > 65536:
        return 1
    return supervise(json.loads(raw), sys.stdin.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
