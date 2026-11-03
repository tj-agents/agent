from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from codex import hook_trust

MAX_RECORD_BYTES = 8 * 1024 * 1024
CONTINUATION = (
    "The user shell command has finished. Read its recorded command, exit status "
    "and output, then respond and continue only existing authorized work. "
    "Do not run the completed command again. Newer user instructions, interrupts, "
    "holds and approval requirements remain in force."
)


@dataclass(frozen=True)
class ShellInput:
    command: str
    submitted_at: float
    epoch: int


@dataclass(frozen=True)
class ShellResult:
    turn_id: str
    item_id: str
    command: str
    started_at: float
    result_recorded: bool = False


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
                        raise RuntimeError("Frontend has no unique startup identity")
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
                if isinstance(turn, dict):
                    self.invalidate()
                    identity = turn.get("client_user_message_id")
                    self.latest = (
                        identity if isinstance(identity, str) and identity else None
                    )
                shell = payload.get("RunUserShellCommand")
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

    def invalidate(self) -> None:
        self.epoch += 1
        self.control_offset = self.stream.offset
        self.latest = None
        self.pending = None


class Rollout:
    def __init__(self, path: Path, profile: Path, thread_id: str) -> None:
        resolved = path.resolve(strict=True)
        sessions = (profile.resolve() / "sessions").resolve()
        if (
            not path.is_absolute()
            or resolved != path
            or not resolved.is_relative_to(sessions)
        ):
            raise RuntimeError("Native rollout is outside its verified profile")
        self.stream = JsonLines(path)
        self.thread_id = thread_id
        self.metadata = False
        self.inputs: set[str] = set()
        self.latest_input: str | None = None
        self.turn_id: str | None = None
        self.standalone = False
        self.shell: ShellResult | None = None
        self.completed = False

    def poll(self) -> None:
        if self.stream.path.resolve(strict=True) != self.stream.path:
            raise RuntimeError("Native rollout path changed after attribution")
        for row in self.stream.read():
            payload = row.get("payload")
            if not isinstance(payload, dict):
                raise RuntimeError("Native rollout payload is invalid")
            if row.get("type") == "session_meta":
                if (
                    self.metadata
                    or payload.get("id") != self.thread_id
                    or payload.get("source") not in ("cli", "vscode")
                    or payload.get("thread_source", "user") != "user"
                ):
                    raise RuntimeError(
                        "Native rollout has a different conversation identity"
                    )
                self.metadata = True
            elif row.get("type") == "event_msg":
                self.event(payload)
            elif row.get("type") == "response_item":
                self.response(payload)

    def event(self, payload: dict[str, Any]) -> None:
        kind = payload.get("type")
        if kind == "task_started":
            identity = payload.get("turn_id")
            if not isinstance(identity, str) or not identity:
                raise RuntimeError("Native turn has no valid identity")
            self.turn_id = identity
            self.standalone = True
            self.shell = None
            self.completed = False
        elif kind == "item_completed":
            item = payload.get("item")
            if not isinstance(item, dict):
                return
            if item.get("type") == "UserMessage":
                self.standalone = False
                identity = item.get("client_id")
                if isinstance(identity, str) and identity:
                    self.inputs.add(identity)
                    self.latest_input = identity
            elif (
                item.get("type") == "CommandExecution"
                and item.get("source") == "user_shell"
            ):
                command, item_id = item.get("command"), item.get("id")
                started = payload.get("started_at_ms")
                if (
                    payload.get("thread_id") == self.thread_id
                    and payload.get("turn_id") == self.turn_id
                    and self.turn_id is not None
                    and isinstance(command, list)
                    and command
                    and isinstance(command[-1], str)
                    and isinstance(item_id, str)
                    and item_id
                    and isinstance(started, int)
                    and item.get("status") in {"completed", "failed"}
                ):
                    self.shell = ShellResult(
                        self.turn_id, item_id, command[-1], started / 1000
                    )
            else:
                self.standalone = False
        elif kind == "task_complete":
            self.completed = bool(
                self.standalone
                and self.shell is not None
                and payload.get("turn_id") == self.turn_id
                and payload.get("last_agent_message") is None
            )
        elif kind == "turn_aborted":
            self.standalone = False
            self.completed = False

    def response(self, payload: dict[str, Any]) -> None:
        if payload.get("role") == "assistant":
            self.standalone = False
            return
        if self.shell is None or payload.get("role") != "user":
            return
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        prefix = (
            "<user_shell_command>\n<command>\n"
            + self.shell.command
            + "\n</command>\n<result>\n"
        )
        content = payload.get("content", [])
        if (
            isinstance(metadata, dict)
            and metadata.get("turn_id") == self.turn_id
            and isinstance(content, list)
            and any(
                isinstance(item, dict)
                and item.get("type") == "input_text"
                and isinstance(item.get("text"), str)
                and item["text"].startswith(prefix)
                for item in content
            )
        ):
            self.shell = ShellResult(
                self.shell.turn_id,
                self.shell.item_id,
                self.shell.command,
                self.shell.started_at,
                True,
            )

    def ready(self, pending: ShellInput, latest: str, own_ids: set[str]) -> bool:
        self.poll()
        return bool(
            self.metadata
            and self.stream.settled()
            and latest in self.inputs
            and self.latest_input in {latest, *own_ids}
            and self.completed
            and self.standalone
            and self.shell is not None
            and self.shell.result_recorded
            and self.shell.command == pending.command
            and self.shell.started_at >= pending.submitted_at
        )


class ShellMonitor:
    def __init__(self, ticket: dict[str, Any], client: Any) -> None:
        self.client = client
        self.ticket = ticket
        self.frontend = FrontendInput(
            Path(ticket["recording_path"]), client.VIEW_CHANGES
        )
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self.run, name="codex-shell-continuation", daemon=True
        )
        self.process: subprocess.Popen[Any] | None = None
        self.rollout: Rollout | None = None
        self.own_ids: set[str] = set()
        self.candidates: dict[str, Rollout] = {}

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.thread.is_alive():
            self.thread.join(timeout=8)
            if self.thread.is_alive():
                raise RuntimeError(
                    "Codex shell observer did not stop before frontend release"
                )

    def run(self) -> None:
        try:
            connection = hook_trust.AppServer(
                self.ticket["_shell_executable"],
                Path(self.ticket["launch_cwd"]),
                environment={"CODEX_HOME": self.ticket["profile"]},
                existing=True,
                timeout=5,
            )
            self.process = connection.process
            with connection as server:
                while not self.stop_event.wait(1):
                    self.discard_notifications(server)
                    try:
                        self.step(server)
                    except FileNotFoundError:
                        if self.frontend.stream.identity is not None:
                            raise
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ) as error:
            print(f"Codex shell continuation stopped: {error}", file=sys.stderr)

    def discard_notifications(self, server: Any) -> None:
        for _ in range(4096):
            try:
                message = server.messages.get_nowait()
            except queue.Empty:
                return
            if message is None or isinstance(message, Exception):
                raise RuntimeError("Native shell observer connection stopped")

    def bind_input(self, latest: str, client_id: str) -> None:
        path = self.frontend.stream.path.with_name("shell-continuation.json")
        record = {
            "client_user_message_id": client_id,
            "anchor": latest,
            "recording_identity": self.frontend.stream.identity,
            "control_offset": self.frontend.control_offset,
            "binding": {
                key: self.ticket[key]
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

    def frontend_matches(self, client: Any, latest: str, pending: ShellInput) -> bool:
        self.frontend.poll()
        ticket = self.ticket
        if (
            self.stop_event.is_set()
            or self.frontend.headers != 1
            or not self.frontend.stream.settled()
            or self.frontend.latest != latest
            or self.frontend.pending != pending
        ):
            return False
        backend = client.daemon_identity(
            {"CODEX_HOME": ticket["profile"]}, expected_pid=ticket["backend"]["pid"]
        )
        if backend != ticket["backend"]:
            return False
        binding = json.loads(
            client.binding_path(ticket["token"]).read_text(encoding="utf-8")
        )
        if any(
            binding.get(key) != ticket[key]
            for key in (
                "token",
                "pid",
                "backend",
                "profile",
                "recording_path",
                "created_at",
            )
        ):
            return False
        context = client.process_context(ticket["pid"])
        arguments = client.process_arguments(ticket["pid"])
        return bool(
            Path(context["profile"]) == Path(ticket["profile"])
            and client.comparable_path(Path(context.get(client.RECORD_PATH_ENV) or ""))
            == client.comparable_path(Path(ticket["recording_path"]))
            and context.get(client.RECORD_ENV) in {"1", "true", "TRUE", "yes", "YES"}
            and arguments
            and client.is_session(arguments[1:])
            and "--no-daemon" not in arguments
            and client.comparable_path(Path(arguments[0]).resolve())
            == client.comparable_path(Path(ticket["_shell_executable"]).resolve())
        )

    def metadata_matches(self, metadata: dict[str, Any], client: Any) -> bool:
        return bool(
            isinstance(metadata.get("cwd"), str)
            and Path(metadata["cwd"]).is_absolute()
            and metadata.get("canAcceptDirectInput") is True
            and metadata.get("source") in ("cli", "vscode")
            and metadata.get("parentThreadId") is None
            and client.comparable_path(Path(metadata.get("cwd", "")).resolve())
            == client.comparable_path(Path(self.ticket["launch_cwd"]).resolve())
        )

    def bind(self, server: Any, client: Any, latest: str) -> None:
        cursor: str | None = None
        seen: set[str] = set()
        matches: list[Rollout] = []
        for _ in range(128):
            params: dict[str, Any] = {"limit": 128}
            if cursor is not None:
                params["cursor"] = cursor
            page = server.request("thread/loaded/list", params)
            for identity in page.get("data", []):
                if not isinstance(identity, str):
                    raise RuntimeError("Native loaded thread identity is invalid")
                metadata = server.request(
                    "thread/read", {"threadId": identity, "includeTurns": False}
                )["thread"]
                if not self.metadata_matches(metadata, client):
                    continue
                path = metadata.get("path")
                if not isinstance(path, str):
                    continue
                rollout = self.candidates.get(identity)
                if rollout is None:
                    rollout = Rollout(
                        Path(path), Path(self.ticket["profile"]), identity
                    )
                    self.candidates[identity] = rollout
                elif rollout.stream.path != Path(path):
                    raise RuntimeError(
                        "Native thread rollout changed after attribution"
                    )
                rollout.poll()
                if latest in rollout.inputs:
                    matches.append(rollout)
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise RuntimeError("Native thread pagination did not advance")
            seen.add(cursor)
        else:
            raise RuntimeError("Native thread attribution exceeds its scan limit")
        if len(matches) > 1:
            raise RuntimeError("Frontend input belongs to multiple native threads")
        if matches:
            self.rollout = matches[0]

    def step(self, server: Any) -> None:
        client = self.client
        self.frontend.poll()
        latest, pending = self.frontend.latest, self.frontend.pending
        if latest is None or pending is None:
            return
        if not self.frontend_matches(client, latest, pending):
            return
        if self.rollout is None or latest not in self.rollout.inputs:
            self.bind(server, client, latest)
        rollout = self.rollout
        if rollout is None or not rollout.ready(pending, latest, self.own_ids):
            return
        metadata = server.request(
            "thread/read", {"threadId": rollout.thread_id, "includeTurns": False}
        )["thread"]
        if (
            not self.metadata_matches(metadata, client)
            or metadata.get("id") != rollout.thread_id
            or metadata.get("status", {}).get("type") != "idle"
            or Path(metadata.get("path", "")) != rollout.stream.path
        ):
            return
        if not self.frontend_matches(client, latest, pending):
            return
        if not rollout.ready(pending, latest, self.own_ids) or rollout.shell is None:
            return
        client_id = str(uuid.uuid4())
        key = hashlib.sha256(
            (
                self.ticket["profile"]
                + "\0"
                + rollout.thread_id
                + "\0"
                + rollout.shell.item_id
            ).encode()
        ).hexdigest()
        state = Path(
            os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state"
        )
        receipts = state / "shell-continuations"
        receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(
                receipts / (key + ".json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            self.frontend.pending = None
            return
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "thread_id": rollout.thread_id,
                    "item_id": rollout.shell.item_id,
                    "client_user_message_id": client_id,
                    "created_at": time.time(),
                },
                stream,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if not self.frontend_matches(client, latest, pending) or not rollout.ready(
            pending, latest, self.own_ids
        ):
            return
        self.bind_input(latest, client_id)
        if not self.frontend_matches(client, latest, pending) or not rollout.ready(
            pending, latest, self.own_ids
        ):
            return
        self.own_ids.add(client_id)
        try:
            server.request(
                "turn/start",
                {
                    "threadId": rollout.thread_id,
                    "clientUserMessageId": client_id,
                    "input": [
                        {"type": "text", "text": CONTINUATION, "text_elements": []}
                    ],
                },
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            print(
                f"Codex shell continuation delivery is uncertain; no replay: {error}",
                file=sys.stderr,
            )
        finally:
            self.frontend.pending = None


def attributed_input(recording: FrontendInput) -> str | None:
    latest = recording.latest
    if latest is None:
        return None
    path = recording.stream.path
    try:
        receipt = json.loads(
            path.with_name("shell-continuation.json").read_text(encoding="utf-8")
        )
        binding = json.loads(path.with_name("client.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return latest
    if not isinstance(receipt, dict) or not isinstance(binding, dict):
        raise RuntimeError("Shell continuation input binding is invalid")
    expected = receipt.get("binding")
    identity = receipt.get("client_user_message_id")
    if (
        isinstance(expected, dict)
        and all(
            key in expected and expected[key] == binding.get(key)
            for key in (
                "token",
                "pid",
                "backend",
                "profile",
                "created_at",
            )
        )
        and receipt.get("anchor") == latest
        and receipt.get("recording_identity") == list(recording.stream.identity or ())
        and receipt.get("control_offset") == recording.control_offset
        and isinstance(identity, str)
        and identity
    ):
        return identity
    return latest


def start(ticket: dict[str, Any], client: Any) -> None:
    monitor = ShellMonitor(ticket, client)
    ticket["_shell_monitor"] = monitor
    monitor.start()


def stop(ticket: dict[str, Any]) -> None:
    monitor = ticket.get("_shell_monitor")
    if isinstance(monitor, ShellMonitor):
        monitor.stop()
        ticket.pop("_shell_monitor", None)
