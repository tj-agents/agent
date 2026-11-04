"""Frontend controls and verified automatic inputs for Codex observers."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_RECORD_BYTES = 8 * 1024 * 1024
_CONTROL_LOCKS: dict[str, threading.RLock] = {}
_CONTROL_GUARD = threading.Lock()


@dataclass(frozen=True)
class ShellInput:
    command: str
    submitted_at: float
    epoch: int


class JsonLines:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.identity: tuple[int, int] | None = None
        self.offset = 0

    def read(self) -> Iterator[dict[str, Any]]:
        stat = self.path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if self.identity is not None and (
            identity != self.identity or stat.st_size < self.offset
        ):
            raise RuntimeError("Native event stream was replaced or truncated")
        self.identity = identity
        with self.path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != identity:
                raise RuntimeError("Native event stream changed while opening")
            stream.seek(self.offset)
            end = self.offset + 4 * MAX_RECORD_BYTES
            while stream.tell() < end:
                line = stream.readline(MAX_RECORD_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_RECORD_BYTES:
                    raise RuntimeError("Native event record exceeds its size limit")
                if not line.endswith(b"\n"):
                    break
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise RuntimeError("Native event record is invalid")
                self.offset = stream.tell()
                yield row

    def settled(self) -> bool:
        stat = self.path.stat()
        if (stat.st_dev, stat.st_ino) != self.identity or stat.st_size < self.offset:
            raise RuntimeError("Native event stream identity changed")
        return stat.st_size == self.offset


class FrontendInput:
    def __init__(self, path: Path, view_changes: set[str]) -> None:
        self.stream = JsonLines(path)
        self.view_changes = view_changes
        self.latest: str | None = None
        self.cwd: Path | None = None
        self.pending: ShellInput | None = None
        self.epoch = 0
        self.control_offset = 0
        self.headers = 0

    def poll(self) -> None:
        for row in self.stream.read():
            if row.get("dir") == "meta":
                if row.get("kind") == "session_start":
                    self.headers += 1
                    if self.headers != 1:
                        raise RuntimeError(
                            "Native frontend recording has no unique startup header"
                        )
                if row.get("kind") in {"session_start", "session_end"}:
                    self.invalidate()
            elif row.get("dir") == "to_tui" and (
                row.get("kind") in {"new_session", "clear_ui"}
                or row.get("variant") in self.view_changes
            ):
                self.invalidate()
            elif row.get("dir") == "from_tui" and row.get("kind") == "op":
                payload = row.get("payload")
                if not isinstance(payload, dict):
                    self.invalidate()
                    continue
                if len(payload) == 1 and set(payload).intersection(
                    {
                        "ExecApproval",
                        "PatchApproval",
                        "ResolveElicitation",
                        "ResolveUserVerification",
                        "UserInputAnswer",
                        "RequestPermissionsResponse",
                        "OverrideTurnContext",
                        "ListSkills",
                        "SetThreadName",
                    }
                ):
                    continue
                if len(payload) != 1 or not {
                    "UserTurn",
                    "RunUserShellCommand",
                }.intersection(payload):
                    self.invalidate()
                    continue
                turn = payload.get("UserTurn")
                if "UserTurn" in payload and not isinstance(turn, dict):
                    self.invalidate()
                    continue
                if isinstance(turn, dict):
                    self.invalidate()
                    identity = turn.get("client_user_message_id")
                    self.latest = (
                        identity if isinstance(identity, str) and identity else None
                    )
                    directory = turn.get("cwd")
                    self.cwd = (
                        Path(directory)
                        if isinstance(directory, str) and Path(directory).is_absolute()
                        else None
                    )
                shell = payload.get("RunUserShellCommand")
                if "RunUserShellCommand" in payload and not (
                    isinstance(shell, dict) and isinstance(shell.get("command"), str)
                ):
                    self.invalidate()
                    continue
                if isinstance(shell, dict) and isinstance(shell.get("command"), str):
                    self.epoch += 1
                    self.control_offset = self.stream.offset
                    self.pending = ShellInput(
                        shell["command"],
                        datetime.fromisoformat(
                            row["ts"].replace("Z", "+00:00")
                        ).timestamp(),
                        self.epoch,
                    )

    def snapshot(self) -> tuple[str | None, int, tuple[int, int]]:
        self.poll()
        if self.stream.identity is None:
            raise RuntimeError("Native frontend recording identity is unavailable")
        return self.latest, self.epoch, self.stream.identity

    def invalidate(self) -> None:
        self.epoch += 1
        self.control_offset = self.stream.offset
        self.latest = None
        self.cwd = None
        self.pending = None


def bind_input(
    recording: FrontendInput, ticket: dict[str, Any], thread_id: str, client_id: str
) -> None:
    path = recording.stream.path.with_name("shell-continuation.json")
    record = {
        "client_user_message_id": client_id,
        "anchor": recording.latest,
        "thread_id": thread_id,
        "recording_identity": recording.stream.identity,
        "control_offset": recording.control_offset,
        "binding": {
            key: ticket[key]
            for key in (
                "token",
                "pid",
                "backend",
                "profile",
                "created_at",
            )
        },
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".shell-continuation-",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _receipt(recording: FrontendInput) -> dict[str, Any] | None:
    if recording.latest is None:
        return None
    path = recording.stream.path
    try:
        receipt = json.loads(
            path.with_name("shell-continuation.json").read_text(encoding="utf-8")
        )
        binding = json.loads(path.with_name("client.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(receipt, dict) or not isinstance(binding, dict):
        raise RuntimeError("Automatic continuation input binding is invalid")
    expected = receipt.get("binding")
    identity = receipt.get("client_user_message_id")
    offset = receipt.get("control_offset")
    if (
        isinstance(expected, dict)
        and all(
            key in expected and expected[key] == binding.get(key)
            for key in ("token", "pid", "backend", "profile", "created_at")
        )
        and receipt.get("anchor") == recording.latest
        and receipt.get("recording_identity") == list(recording.stream.identity or ())
        and isinstance(offset, int)
        and 0 <= offset <= recording.control_offset
        and isinstance(identity, str)
        and identity
    ):
        return receipt
    return None


def attributed_input(recording: FrontendInput) -> str | None:
    receipt = _receipt(recording)
    if receipt is not None and receipt["control_offset"] == recording.control_offset:
        return str(receipt["client_user_message_id"])
    return recording.latest


def observer_input(recording: FrontendInput, thread_id: str) -> str | None:
    # Only shell controls preserve the manual anchor while advancing the epoch.
    receipt = _receipt(recording)
    if receipt is not None and receipt.get("thread_id") == thread_id:
        return str(receipt["client_user_message_id"])
    return recording.latest


def shell_pending(recording: FrontendInput, thread_id: str) -> bool:
    if recording.pending is None:
        return False
    receipt = _receipt(recording)
    return not (
        receipt is not None
        and receipt.get("thread_id") == thread_id
        and receipt["control_offset"] == recording.control_offset
    )


def _control_key(ticket: dict[str, Any]) -> str:
    return str(ticket.get("token") or ticket.get("recording_path") or id(ticket))


def control_lock(ticket: dict[str, Any]) -> threading.RLock:
    with _CONTROL_GUARD:
        return _CONTROL_LOCKS.setdefault(_control_key(ticket), threading.RLock())


def release_control(ticket: dict[str, Any]) -> None:
    with _CONTROL_GUARD:
        _CONTROL_LOCKS.pop(_control_key(ticket), None)
