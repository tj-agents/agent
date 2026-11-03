"""Settle one completed Agent turn, then authorize its exact frontend close."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import session_close

_LIFECYCLE = None


def lifecycle():
    global _LIFECYCLE
    if _LIFECYCLE is not None:
        return _LIFECYCLE
    path = Path(__file__).resolve().parents[3] / "runtime" / "close_lifecycle.py"
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("terminal_release_lifecycle", path)
    if spec is None or spec.loader is None:
        raise session_close.Refusal("the packaged terminal release runtime is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _LIFECYCLE = module
    return _LIFECYCLE


def canonical_path(request):
    session = request.get("session_id")
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session):
        raise session_close.Refusal("invalid close session identity")
    return session_close.state_directory() / "terminal-release" / (session + ".json")


def load_request(path, digest):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise session_close.Refusal("the frozen close request changed")
    request = json.loads(raw)
    if (not isinstance(request, dict) or request.get("version") != 1
            or request.get("status") != "armed"
            or not isinstance(request.get("entry"), dict)
            or not isinstance(request.get("completion_token"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request["completion_token"])):
        raise session_close.Refusal("invalid close request")
    expected = session_close.state_directory() / "terminal-release/requests" / (request["completion_token"] + ".json")
    if path.resolve() != expected.resolve():
        raise session_close.Refusal("close request is outside canonical state")
    return request


def require_current(request, path, digest, *, verify_receipt=False):
    if load_request(path, digest) != request or session_close.read_json(canonical_path(request)) != request:
        raise session_close.Cancelled("close request generation changed")
    if Path(request["cancel"]).exists():
        raise session_close.Cancelled("close request was cancelled")
    try:
        lifecycle().require_release(request, verify_receipt=verify_receipt)
    except (OSError, ValueError, RuntimeError) as error:
        raise session_close.Cancelled(str(error)) from error


def acknowledge(request, path, digest):
    require_current(request, path, digest, verify_receipt=True)
    identity = session_close.process_info(os.getpid())
    if identity is None:
        raise session_close.Refusal("terminal release watcher identity is unavailable")
    acknowledgement = Path(request["acknowledgement"])
    acknowledgement.parent.mkdir(parents=True, exist_ok=True)
    try:
        with acknowledgement.open("x", encoding="utf-8") as stream:
            json.dump({"request_sha256": digest, "pid": identity.pid,
                       "started_at": identity.started_at}, stream, sort_keys=True)
            stream.write("\n")
    except FileExistsError:
        existing = session_close.read_json(acknowledgement)
        if existing != {"request_sha256": digest, "pid": identity.pid,
                        "started_at": identity.started_at}:
            raise session_close.Cancelled(
                "terminal release acknowledgement changed"
            ) from None


def complete(request):
    value = session_close.read_json(request["completion"])
    return isinstance(value, dict) and value.get("token") == request["completion_token"]


def final_local_guard(request, path, digest, activity):
    require_current(request, path, digest, verify_receipt=True)
    entry = request["entry"]
    current = session_close.read_json(request["registry"])
    if not session_close.registration_matches(current, entry):
        raise session_close.Cancelled("session registration changed before close")
    session_close.host_sessions(request["host"]).close_guard(request, activity, complete(request), session_close.SHARED)
    require_current(request, path, digest)


def _record_end(entry):
    module = session_close.register_session._load_session_store()
    if module is None:
        raise session_close.Refusal("session store is unavailable")
    with module.SessionStore() as store:
        store.update_session(module.session_handle(entry["host"], entry["session_id"]),
                             lambda session: session.end("closed"))


def closed_after_stop(entry, guard):
    """Return a secondary Store diagnostic only after a verified client stop."""
    session_close.stop_cli_client(entry, guard)
    try:
        _record_end(entry)
    except Exception as error:  # Store bookkeeping cannot undo a verified client exit.
        return f"session Store bookkeeping failed after close: {error}"
    return None


def wait(request, path, digest, timeout=600.0, idle=3.0, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    marker = since = None
    try:
        acknowledge(request, path, digest)
        sessions = session_close.host_sessions(request["host"])
        monitor = sessions.close_monitor(request, session_close.SHARED)
        while clock() < deadline:
            require_current(request, path, digest)
            settled = sessions.settled(request, monitor, complete(request), session_close.SHARED)
            if settled is None:
                since = None
            else:
                if since is None:
                    marker, since = settled, clock()
                elif settled != marker:
                    raise session_close.Cancelled("a later turn replaced the completed turn")
                if clock() - since >= idle:
                    def guard(marker=marker):
                        final_local_guard(request, path, digest, marker)

                    reason = closed_after_stop(request["entry"], guard)
                    status = "closed"
                    break
            sleep(0.05)
        else:
            status, reason = "timeout", "the armed turn did not settle before timeout"
    except session_close.Cancelled as error:
        status, reason = "cancelled", str(error)
    except (OSError, ValueError, RuntimeError) as error:
        status = "cancelled" if request.get("intent") == "refresh-restart" else "refused"
        reason = str(error)
    if request.get("intent") == "refresh-restart" and status != "closed":
        session_close.write_json(request["cancel"], {"cancelled": True, "reason": reason})
    session_close.write_json(request["result"], {"status": status, "reason": reason,
                              "session_id": request["session_id"], "at": time.time()})
    lifecycle().retire_release(request)
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
