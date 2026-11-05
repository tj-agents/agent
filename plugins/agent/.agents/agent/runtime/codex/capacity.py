"""Recover a managed Codex frontend from the exact transient capacity failure.

Recovery is limited to a frontend attached to the shared managed app-server. Codex's
turn/start has no compare-and-swap against the failed turn, so a small race remains
between the final input check and native acceptance; the monitor rechecks immediately
before sending and never claims that cancellation is atomic.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

CAPACITY_ERROR = "Selected model is at capacity. Please try a different model."
DELAYS = (5, 15, 30, 60, 120)
RECOVERY_TEXT = (
    "Codex capacity failure. Continue only unfinished authorized work. "
    "Existing holds and approvals remain in force. Newer user instructions "
    "or interrupts take precedence."
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def state_root() -> Path:
    return Path(
        os.environ.get("AGENT_STATE_DIRECTORY") or Path.home() / ".agents-state"
    )


def candidate_path(
    profile: str, thread_id: str, turn_id: str, root: Path | None = None
) -> Path:
    base = root or state_root()
    return (
        base
        / "capacity-inputs"
        / digest(str(Path(profile).resolve()))
        / (digest(thread_id + "\0" + turn_id) + ".json")
    )


def write_candidate(payload: dict[str, Any], root: Path | None = None) -> bool:
    thread_id, turn_id, cwd = (
        payload.get("session_id"),
        payload.get("turn_id"),
        payload.get("cwd"),
    )
    profile = (
        payload.get("codex_home")
        or os.environ.get("CODEX_HOME")
        or str(Path.home() / ".codex")
    )
    if not all(
        isinstance(value, str) and value and len(value) <= 512
        for value in (thread_id, turn_id, cwd)
    ):
        return False
    if (
        not isinstance(thread_id, str)
        or not isinstance(turn_id, str)
        or not isinstance(cwd, str)
    ):
        return False
    if not Path(cwd).is_absolute():
        return False
    path = candidate_path(profile, thread_id, turn_id, root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = {
        "version": 1,
        "profile": str(Path(profile).resolve()),
        "thread_id": thread_id,
        "turn_id": turn_id,
        "cwd": str(Path(cwd).resolve()),
        "created_at": time.time(),
    }
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, separators=(",", ":"))
            stream.write("\n")
        with contextlib.suppress(FileExistsError):
            os.link(temporary, path)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def hook_main() -> int:
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            write_candidate(payload)
    except (OSError, ValueError, TypeError):
        print(
            "Codex capacity hook could not record a candidate; no recovery sent.",
            file=sys.stderr,
        )
    return 0


def capacity_failure(turn: dict[str, Any]) -> bool:
    if turn.get("status") != "failed":
        return False
    error = turn.get("error")
    if not isinstance(error, dict):
        return False
    code = error.get("codexErrorInfo")
    if code is not None:
        return bool(code == "serverOverloaded")
    return bool(error.get("message") == CAPACITY_ERROR)


def pages(server: Any, thread_id: str, turn_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(128):
        params: dict[str, Any] = {
            "threadId": thread_id,
            "turnId": turn_id,
            "limit": 100,
            "sortDirection": "desc",
        }
        if cursor is not None:
            params["cursor"] = cursor
        page = server.request("thread/items/list", params)
        data = page.get("data")
        if not isinstance(data, list):
            raise RuntimeError("Codex history page is invalid")
        result.extend(
            entry["item"]
            for entry in data
            if isinstance(entry, dict) and isinstance(entry.get("item"), dict)
        )
        cursor = page.get("nextCursor")
        if cursor is None:
            return result
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise RuntimeError("Codex history pagination did not advance")
        seen.add(cursor)
    raise RuntimeError("Codex history exceeds the attribution scan limit")


def user_ids(server: Any, thread_id: str, turn_id: str) -> set[str]:
    return {
        item["clientId"]
        for item in pages(server, thread_id, turn_id)
        if item.get("type") == "userMessage" and isinstance(item.get("clientId"), str)
    }


def contains_client_id(server: Any, thread_id: str, client_id: str) -> bool:
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(128):
        params: dict[str, Any] = {
            "threadId": thread_id,
            "limit": 128,
            "sortDirection": "desc",
            "itemsView": "notLoaded",
        }
        if cursor is not None:
            params["cursor"] = cursor
        page = server.request("thread/turns/list", params)
        data = page.get("data")
        if not isinstance(data, list):
            return False
        for turn in data:
            if (
                isinstance(turn, dict)
                and isinstance(turn.get("id"), str)
                and client_id in user_ids(server, thread_id, turn["id"])
            ):
                return True
        cursor = page.get("nextCursor")
        if cursor is None:
            return False
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            return False
        seen.add(cursor)
    return False


class CapacityMonitor:
    def __init__(self, ticket: dict[str, Any], clock: Any = time.monotonic):
        from codex import frontend_input

        self.ticket = ticket
        self.clock = clock
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self.run, name="codex-capacity-recovery", daemon=True
        )
        self.process: subprocess.Popen[str] | None = None
        self.recording = frontend_input.FrontendInput(
            Path(ticket.get("recording_path") or os.devnull), set()
        )
        self.started_at = float(ticket["created_at"])
        self.attempts: dict[str, int] = {}
        self.receipts: set[str] = set()
        self.auto_ids: set[str] = set()
        self.disabled = False
        self.binding_key: tuple[str, int] | None = None
        self.bound_candidate: dict[str, Any] | None = None

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
                    "Codex capacity monitor did not stop before frontend release"
                )

    def run(self) -> None:
        try:
            self._run()
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ) as error:
            print(f"Codex capacity recovery stopped safely: {error}", file=sys.stderr)

    def _run(self) -> None:
        from codex import client

        self.recording.view_changes = client.VIEW_CHANGES
        ticket = self.ticket
        if not ticket.get("backend") or not ticket.get("_capacity_executable"):
            return
        launch_args = ticket.get("_capacity_launch_args", [])
        server_context = client.observer_server(
            ticket, ticket["_capacity_executable"], launch_args=launch_args, timeout=15
        )
        self.process = server_context.process
        with server_context as server:
            client.observer_binding(ticket)
            while not self.stop_event.wait(2):
                try:
                    latest, epoch, identity = self.recording.snapshot()
                except FileNotFoundError:
                    if self.recording.stream.identity is None:
                        continue
                    raise
                expected = ticket.get("_capacity_record_identity")
                if expected is None:
                    ticket["_capacity_record_identity"] = identity
                elif identity != expected:
                    return
                if latest is None:
                    self.binding_key = None
                    self.bound_candidate = None
                    continue
                ticket["_capacity_epoch"] = epoch
                candidate = self._bound_candidate_for(server, latest, epoch)
                if candidate is not None:
                    self._recover(server, client, candidate, latest, epoch)

    def _backend_matches(self, client: Any) -> bool:
        try:
            client.observer_binding(self.ticket)
        except (OSError, RuntimeError, ValueError, TypeError):
            return False
        return True

    def _candidates(self) -> list[dict[str, Any]]:
        directory = state_root() / "capacity-inputs" / digest(self.ticket["profile"])
        if not directory.is_dir():
            return []
        candidates: list[dict[str, Any]] = []
        for path in sorted(
            directory.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[:128]:
            try:
                if path.stat().st_size > 8192:
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
                if (
                    isinstance(value, dict)
                    and value.get("version") == 1
                    and value.get("profile") == self.ticket["profile"]
                    and value.get("cwd")
                    == str(Path(self.ticket["launch_cwd"]).resolve())
                    and value.get("created_at", 0) >= self.started_at
                    and isinstance(value.get("thread_id"), str)
                    and isinstance(value.get("turn_id"), str)
                ):
                    candidates.append(value)
            except (OSError, ValueError, TypeError):
                continue
        return candidates

    def _bound_candidate_for(
        self, server: Any, latest: str, epoch: int
    ) -> dict[str, Any] | None:
        from codex import frontend_input

        active = frontend_input.attributed_input(self.recording) or latest
        key = (active, epoch)
        if key != self.binding_key:
            self.binding_key = key
            self.bound_candidate = None
        if self.bound_candidate is None:
            matches = [
                value
                for value in self._candidates()
                if not frontend_input.shell_pending(self.recording, value["thread_id"])
                and self._candidate_matches(server, value, active)
            ]
            if not matches and active != latest:
                matches = [
                    value
                    for value in self._candidates()
                    if not frontend_input.shell_pending(
                        self.recording, value["thread_id"]
                    )
                    and self._candidate_matches(server, value, latest)
                ]
            if len(matches) == 1:
                self.bound_candidate = matches[0]
        return self.bound_candidate

    def _candidate_matches(
        self, server: Any, candidate: dict[str, Any], latest: str
    ) -> bool:
        if candidate["cwd"] != str(Path(self.ticket["launch_cwd"]).resolve()):
            return False
        items = pages(server, candidate["thread_id"], candidate["turn_id"])
        if not any(
            item.get("type") == "userMessage" and item.get("clientId") == latest
            for item in items
        ):
            return False
        cursor: str | None = None
        seen: set[str] = set()
        loaded_match = False
        for _ in range(128):
            params: dict[str, Any] = {"limit": 128}
            if cursor is not None:
                params["cursor"] = cursor
            loaded = server.request("thread/loaded/list", params)
            threads = loaded.get("data", [])
            if isinstance(threads, list) and candidate["thread_id"] in threads:
                loaded_match = True
                break
            cursor = loaded.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                return False
            seen.add(cursor)
        if not loaded_match:
            return False
        thread_result = server.request(
            "thread/read", {"threadId": candidate["thread_id"], "includeTurns": False}
        )
        thread = thread_result.get("thread", {})
        source = thread.get("source")
        thread_source = thread.get("threadSource")
        background = thread_source in {
            "subAgent",
            "subAgentReview",
            "subAgentCompact",
            "subAgentThreadSpawn",
            "subAgentOther",
        } or (isinstance(source, dict) and "subAgent" in source)
        frontend_source = source in ("cli", "vscode")
        return bool(
            thread.get("id") == candidate["thread_id"]
            and thread.get("cwd") == candidate["cwd"]
            and thread.get("canAcceptDirectInput") is True
            and not background
            and frontend_source
        )

    def _recover(
        self,
        server: Any,
        client: Any,
        candidate: dict[str, Any],
        latest: str,
        epoch: int,
    ) -> None:
        from codex import frontend_input

        with frontend_input.control_lock(self.ticket):
            self._recover_locked(server, client, candidate, latest, epoch)

    def _recover_locked(
        self,
        server: Any,
        client: Any,
        candidate: dict[str, Any],
        latest: str,
        epoch: int,
    ) -> None:
        from codex import frontend_input

        thread_id = candidate["thread_id"]
        self.recording.poll()
        if frontend_input.shell_pending(self.recording, thread_id):
            return
        active = frontend_input.observer_input(self.recording, thread_id) or latest
        if active != latest:
            self.auto_ids.add(active)
        turns = server.request(
            "thread/turns/list",
            {
                "threadId": thread_id,
                "limit": 1,
                "sortDirection": "desc",
                "itemsView": "notLoaded",
            },
        ).get("data")
        if not isinstance(turns, list) or not turns or not isinstance(turns[0], dict):
            return
        failed = turns[0]
        turn_id = failed.get("id")
        if not isinstance(turn_id, str) or not turn_id or not capacity_failure(failed):
            return
        attempt = self.attempts.get(latest, 0)
        if attempt >= len(DELAYS):
            if latest not in self.receipts:
                print(
                    "Recovery exhausted five attempts; session remains available.",
                    file=sys.stderr,
                )
                self.receipts.add(latest)
            return
        deadline = self.clock() + DELAYS[attempt]
        while self.clock() < deadline:
            if self.stop_event.wait(min(0.25, max(0, deadline - self.clock()))):
                return
            current, current_epoch, _identity = self.recording.snapshot()
            if current != latest or current_epoch != epoch:
                return
        if not self._backend_matches(client) or not self._frontend_matches(
            client, latest, epoch
        ):
            return
        if not self._candidate_matches(server, candidate, active) and (
            active == latest or not self._candidate_matches(server, candidate, latest)
        ):
            return
        current_turns = server.request(
            "thread/turns/list",
            {
                "threadId": thread_id,
                "limit": 1,
                "sortDirection": "desc",
                "itemsView": "notLoaded",
            },
        ).get("data")
        if (
            not isinstance(current_turns, list)
            or not current_turns
            or current_turns[0].get("id") != turn_id
            or not capacity_failure(current_turns[0])
        ):
            return
        own_ids = user_ids(server, thread_id, turn_id)
        if latest not in own_ids and not (own_ids & self.auto_ids):
            return
        receipt_root = state_root() / "capacity-recovery"
        receipt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = digest(self.ticket["profile"] + "\0" + thread_id + "\0" + turn_id)
        receipt_path = receipt_root / (key + ".json")
        client_id = str(uuid.uuid4())
        receipt = {
            "version": 1,
            "profile": self.ticket["profile"],
            "thread_id": thread_id,
            "failed_turn_id": turn_id,
            "client_user_message_id": client_id,
            "created_at": time.time(),
        }
        try:
            descriptor = os.open(
                receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            if receipt_path.stat().st_size > 8192:
                return
            existing = json.loads(receipt_path.read_text(encoding="utf-8"))
            existing_id = (
                existing.get("client_user_message_id")
                if isinstance(existing, dict)
                else None
            )
            if isinstance(existing_id, str) and contains_client_id(
                server, thread_id, existing_id
            ):
                return
            return
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        with contextlib.suppress(OSError):
            directory_fd = os.open(receipt_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        self.attempts[latest] = attempt + 1
        self.auto_ids.add(client_id)
        if not self._backend_matches(client) or not self._frontend_matches(
            client, latest, epoch
        ):
            return
        if not self._final_preflight(server, candidate, latest, turn_id):
            return
        if not self._backend_matches(client) or not self._frontend_record_matches(
            client, latest, epoch
        ):
            return
        frontend_input.bind_input(self.recording, self.ticket, thread_id, client_id)
        if not self._backend_matches(client) or not self._frontend_record_matches(
            client, latest, epoch
        ):
            return
        try:
            server.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "clientUserMessageId": client_id,
                    "input": [
                        {"type": "text", "text": RECOVERY_TEXT, "text_elements": []}
                    ],
                },
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            try:
                observed = contains_client_id(server, thread_id, client_id)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError):
                observed = False
            if observed:
                print(
                    "Codex accepted the reserved recovery input; "
                    "no repeat will be sent.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Codex recovery delivery is unknown ({error}); "
                    "receipt retained, no repeat will be sent.",
                    file=sys.stderr,
                )

    def _frontend_record_matches(self, client: Any, latest: str, epoch: int) -> bool:
        current, current_epoch, identity = self.recording.snapshot()
        expected = self.ticket.get("_capacity_record_identity")
        if (
            current != latest
            or current_epoch != epoch
            or identity != expected
            or self.recording.headers != 1
            or not self.recording.stream.settled()
            or self.stop_event.is_set()
        ):
            return False
        try:
            context = client.process_context(self.ticket["pid"])
            binding = json.loads(
                client.binding_path(self.ticket["token"]).read_text(encoding="utf-8")
            )
        except (OSError, RuntimeError, ValueError, TypeError):
            return False
        binding_matches = (
            isinstance(binding, dict)
            and binding.get("token") == self.ticket["token"]
            and binding.get("pid") == self.ticket["pid"]
            and binding.get("backend") == self.ticket["backend"]
            and binding.get("profile") == self.ticket["profile"]
            and binding.get("recording_path") == self.ticket["recording_path"]
            and binding.get("created_at") == self.ticket["created_at"]
        )
        return bool(
            binding_matches
            and Path(context["profile"]) == Path(self.ticket["profile"])
            and client.comparable_path(Path(context.get(client.RECORD_PATH_ENV) or ""))
            == client.comparable_path(Path(self.ticket["recording_path"]))
            and context.get(client.RECORD_ENV) in ("1", "true", "TRUE", "yes", "YES")
        )

    def _frontend_matches(self, client: Any, latest: str, epoch: int) -> bool:
        if not self._frontend_record_matches(client, latest, epoch):
            return False
        try:
            arguments = client.process_arguments(self.ticket["pid"])
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return False
        executable = self.ticket.get("_capacity_executable")
        if not isinstance(executable, str) or not arguments:
            return False
        try:
            actual_executable = client.comparable_path(Path(arguments[0]).resolve())
            expected_executable = client.comparable_path(Path(executable).resolve())
        except (OSError, RuntimeError, ValueError):
            return False
        return bool(
            actual_executable == expected_executable
            and client.is_session(arguments[1:])
            and "--no-daemon" not in arguments
        )

    def _final_preflight(
        self, server: Any, candidate: dict[str, Any], latest: str, turn_id: str
    ) -> bool:
        thread_id = candidate["thread_id"]
        result = server.request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        thread = result.get("thread", {})
        source = thread.get("source")
        if (
            thread.get("id") != thread_id
            or thread.get("cwd") != candidate["cwd"]
            or thread.get("canAcceptDirectInput") is not True
            or not isinstance(thread.get("status"), dict)
            # Codex retains systemError after a failed turn until the next turn starts.
            or thread["status"].get("type") not in {"idle", "systemError"}
            or source not in ("cli", "vscode")
        ):
            return False
        turns = server.request(
            "thread/turns/list",
            {
                "threadId": thread_id,
                "limit": 1,
                "sortDirection": "desc",
                "itemsView": "notLoaded",
            },
        ).get("data")
        if (
            not isinstance(turns, list)
            or not turns
            or not isinstance(turns[0], dict)
            or turns[0].get("id") != turn_id
            or not capacity_failure(turns[0])
        ):
            return False
        current_ids = user_ids(server, thread_id, turn_id)
        return latest in current_ids or bool(current_ids & self.auto_ids)


def start(ticket: dict[str, Any]) -> None:
    monitor = CapacityMonitor(ticket)
    ticket["_capacity_monitor"] = monitor
    monitor.start()


def stop(ticket: dict[str, Any]) -> None:
    monitor = ticket.get("_capacity_monitor")
    if isinstance(monitor, CapacityMonitor):
        monitor.stop()
        ticket.pop("_capacity_monitor", None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("candidate",))
    parser.parse_args()
    return hook_main()


if __name__ == "__main__":
    raise SystemExit(main())
