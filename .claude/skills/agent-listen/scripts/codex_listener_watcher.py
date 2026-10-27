#!/usr/bin/env python3
"""Run one frozen listener command and return its result to its Codex thread."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid


OUTPUT_LIMIT = 6000


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_request(path: Path, digest: str) -> dict:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("listener request changed before startup")
    value = json.loads(raw)
    required = {"schema_version", "listener_id", "state", "created_at", "session", "purpose", "timeout", "command"}
    if not isinstance(value, dict) or set(value) != required or value["schema_version"] != 1:
        raise ValueError("listener request is invalid")
    session = value["session"]
    fields = ("session_id", "host", "cwd", "profile", "recording_path", "client_message_id")
    if (not isinstance(session, dict) or session.get("host") != "codex"
            or any(not isinstance(session.get(key), str) or not session[key] for key in fields)):
        raise ValueError("listener has no valid Codex session binding")
    if not isinstance(value["command"], list) or not value["command"] or any(not isinstance(item, str) or not item for item in value["command"]):
        raise ValueError("listener command is invalid")
    return value


def codex_client():
    here = Path(__file__).resolve()
    for root in (here.parent, *here.parents):
        runtime = root / ".agents" / "agent" / "runtime"
        path = runtime / "codex" / "client.py"
        if path.is_file():
            sys.path.insert(0, str(runtime))
            sys.path.insert(0, str(path.parent))
            spec = importlib.util.spec_from_file_location("codex_listener_client", path)
            module = importlib.util.module_from_spec(spec)
            if spec is None or spec.loader is None:
                break
            spec.loader.exec_module(module)
            return module
    raise RuntimeError("the packaged Codex client runtime is unavailable")


def pages(server, method: str, parameters: dict):
    cursor = None
    seen = set()
    while True:
        result = server.request(method, {**parameters, "cursor": cursor, "limit": 100})
        values = result.get("data")
        if not isinstance(values, list):
            raise RuntimeError(f"{method} returned an unsupported response")
        yield from values
        cursor = result.get("nextCursor")
        if cursor is None:
            return
        if not isinstance(cursor, str) or cursor in seen:
            raise RuntimeError(f"{method} pagination did not advance")
        seen.add(cursor)


def current_input(client, request: dict) -> bool:
    session = request["session"]
    try:
        return client.latest_input(session["recording_path"]) == session["client_message_id"]
    except (OSError, RuntimeError, ValueError):
        return False


def compact(text: str) -> str:
    return text[-OUTPUT_LIMIT:] if len(text) > OUTPUT_LIMIT else text


def run_command(client, request: dict) -> dict:
    deadline = time.monotonic() + request["timeout"]
    command = subprocess.Popen(request["command"], cwd=request["session"]["cwd"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               errors="replace")
    while True:
        try:
            stdout, stderr = command.communicate(timeout=0.25)
            return {"status": "completed", "returncode": command.returncode, "stdout": compact(stdout), "stderr": compact(stderr)}
        except subprocess.TimeoutExpired:
            if not current_input(client, request):
                command.terminate()
                stdout, stderr = command.communicate(timeout=10)
                return {"status": "cancelled", "reason": "the owning Codex input changed", "stdout": compact(stdout), "stderr": compact(stderr)}
            if time.monotonic() >= deadline:
                command.terminate()
                stdout, stderr = command.communicate(timeout=10)
                return {"status": "timeout", "reason": "listener command reached its timeout", "stdout": compact(stdout), "stderr": compact(stderr)}


def deliver(client, request: dict, result: dict) -> dict:
    if result["status"] == "cancelled":
        return result
    if not current_input(client, request):
        return {**result, "delivery": "cancelled", "reason": "the owning Codex input changed"}
    session = request["session"]
    cwd, profile = Path(session["cwd"]), Path(session["profile"])
    message = "\n".join([
        f"Background listener completed: {request['purpose']}",
        f"Listener ID: {request['listener_id']}",
        f"Status: {result['status']}",
        *([f"Exit code: {result['returncode']}"] if "returncode" in result else []),
        *([f"Reason: {result['reason']}"] if "reason" in result else []),
        *(["stdout:\n" + result["stdout"]] if result.get("stdout") else []),
        *(["stderr:\n" + result["stderr"]] if result.get("stderr") else []),
    ])
    from codex import hook_trust

    with hook_trust.AppServer("codex", cwd, environment={"CODEX_HOME": str(profile)}, timeout=20, existing=True) as server:
        thread = server.request("thread/read", {"threadId": session["session_id"]}).get("thread")
        if not isinstance(thread, dict) or thread.get("id") != session["session_id"] or Path(thread.get("cwd", "")).resolve() != cwd.resolve():
            raise RuntimeError("listener thread identity changed")
        loaded = list(pages(server, "thread/loaded/list", {}))
        if session["session_id"] not in loaded or thread.get("canAcceptDirectInput") is not True:
            raise RuntimeError("listener thread cannot accept a completion notification")
        parameters = {"threadId": session["session_id"], "clientUserMessageId": str(uuid.uuid4()),
                      "input": [{"type": "text", "text": message, "text_elements": []}]}
        state = (thread.get("status") or {}).get("type")
        if state == "active":
            active = [turn for turn in pages(server, "thread/turns/list", {"threadId": session["session_id"], "itemsView": "notLoaded"}) if turn.get("status") == "inProgress"]
            if len(active) != 1:
                raise RuntimeError("listener cannot identify the active Codex turn")
            server.request("turn/steer", {**parameters, "expectedTurnId": active[0]["id"]})
        elif state == "idle":
            server.request("turn/start", parameters)
        else:
            raise RuntimeError("listener thread is not available for notification")
    return {**result, "delivery": "sent"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    arguments = parser.parse_args(argv)
    request = load_request(arguments.request, arguments.request_sha256)
    write_json(arguments.request, {**request, "state": "running", "started_at": time.time()})
    try:
        client = codex_client()
        result = deliver(client, request, run_command(client, request))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        result = {"status": "unavailable", "reason": str(error)}
    write_json(arguments.request, {**request, "state": "finished", "finished_at": time.time(), "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
