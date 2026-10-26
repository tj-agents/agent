"""Settle one completed Agent turn, then authorize its exact frontend close."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import time

import session_close


class Cancelled(session_close.Refusal):
    pass


def canonical_path(request):
    session = request.get("session_id")
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session):
        raise session_close.Refusal("invalid close session identity")
    return session_close.state_directory() / "close-lifecycle" / (session + ".json")


def load_request(path, digest):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise session_close.Refusal("the frozen close request changed")
    request = json.loads(raw)
    if (not isinstance(request, dict) or request.get("mode") != "close"
            or not isinstance(request.get("entry"), dict)
            or not isinstance(request.get("completion_token"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request["completion_token"])):
        raise session_close.Refusal("invalid close request")
    expected = session_close.state_directory() / "client-close" / (request["completion_token"] + ".json")
    if path.resolve() != expected.resolve():
        raise session_close.Refusal("close request is outside canonical state")
    return request


def require_current(request, path, digest):
    if load_request(path, digest) != request or session_close.read_json(canonical_path(request)) != request:
        raise Cancelled("close request generation changed")
    if Path(request["cancel"]).exists():
        raise Cancelled("close request was cancelled")


def complete(request):
    value = session_close.read_json(request["completion"])
    return isinstance(value, dict) and value.get("token") == request["completion_token"]


def final_local_guard(request, path, digest, activity):
    require_current(request, path, digest)
    if request["host"] != "codex" and not complete(request):
        raise Cancelled("completion changed before close")
    entry = request["entry"]
    current = session_close.read_json(request["registry"])
    fields = (*session_close.CLIENT_BINDING_FIELDS, "cwd", "frontend", "provenance", "terminal")
    if not isinstance(current, dict) or any(current.get(key) != entry.get(key) for key in fields):
        raise Cancelled("session registration changed before close")
    if request["host"] == "claude":
        lifecycle = session_close._load_runtime("close_lifecycle.py", "agent_close_lifecycle")
        if not lifecycle._claude_finished(request, {"transcript_path": request["transcript"]}):
            raise Cancelled("Claude input or final response changed before close")
    if request["host"] == "codex":
        if not activity.poll():
            raise Cancelled("the bound native turn is no longer complete")
    else:
        session_close.awaiting_input_activity(entry, activity)


def _record_end(entry):
    store = session_close.register_session._load_session_store()
    if store is None:
        raise session_close.Refusal("session store is unavailable")
    connection = store.connect()
    try:
        store.end_session(connection, store.session_handle(entry["host"], entry["session_id"]), "closed")
    finally:
        connection.close()


def closed_after_stop(entry, guard, cli_root):
    """Return a secondary Store diagnostic only after a verified client stop."""
    session_close.stop_cli_client(entry, guard, cli_root)
    try:
        _record_end(entry)
    except Exception as error:  # Store bookkeeping cannot undo a verified client exit.
        return f"session Store bookkeeping failed after close: {error}"
    return None


def native_completion(request):
    pin = request.get("native_completion")
    if not isinstance(pin, dict):
        raise session_close.Refusal("the native close binding is missing")
    return session_close.client_runtime().NativeCompletion(request["entry"], pin)


def wait(request, path, digest, timeout=600.0, idle=3.0, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    stopped = since = None
    try:
        guard = native_completion(request) if request["host"] == "codex" else None
        while clock() < deadline:
            require_current(request, path, digest)
            if guard is not None:
                try:
                    finished = guard.poll()
                except RuntimeError as error:
                    raise Cancelled(str(error)) from error
                if finished:
                    if since is None:
                        since = clock()
                    elif clock() - since >= idle:
                        def native_guard():
                            try:
                                final_local_guard(request, path, digest, guard)
                            except RuntimeError as error:
                                raise Cancelled(str(error)) from error

                        reason = closed_after_stop(request["entry"], native_guard, request["cli_root"])
                        status = "closed"
                        break
                else:
                    since = None
                sleep(0.05)
                continue
            completed = complete(request)
            row = session_close.session_activity(request["entry"])
            if completed:
                if row["turn_state"] != "awaiting_input" or row["last_activity_at"] <= request["armed_at"]:
                    raise Cancelled("completed turn is no longer awaiting input")
                if stopped is None:
                    stopped, since = row["last_activity_at"], clock()
                elif row["last_activity_at"] != stopped:
                    raise Cancelled("a later turn replaced the completed turn")
                if clock() - since >= idle:
                    def guard():
                        final_local_guard(request, path, digest, stopped)

                    reason = closed_after_stop(request["entry"], guard, request["cli_root"])
                    status = "closed"
                    break
            elif row["last_activity_at"] != request["working_activity"]:
                # Store's Stop runs before the completion hook. Only this transient idle row may wait.
                if row["turn_state"] != "awaiting_input":
                    raise Cancelled("the armed working turn changed")
            sleep(0.05)
        else:
            status, reason = "timeout", "the armed turn did not settle before timeout"
    except Cancelled as error:
        status, reason = "cancelled", str(error)
    except (OSError, ValueError, RuntimeError) as error:
        status, reason = "refused", str(error)
    session_close.write_json(request["result"], {"status": status, "reason": reason,
                              "session_id": request["session_id"], "at": time.time()})
    state = canonical_path(request)
    if session_close.read_json(state) == request:
        state.unlink(missing_ok=True)
    return 0 if status in ("closed", "cancelled") else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        request = load_request(args.request, args.request_sha256)
        return wait(request, args.request, args.request_sha256)
    except (OSError, ValueError, RuntimeError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
