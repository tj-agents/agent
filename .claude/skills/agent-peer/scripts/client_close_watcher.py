"""Settle one completed Agent turn, then authorize its exact frontend close."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import time

import session_close


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
    expected = session_close.state_directory() / "close-lifecycle/requests" / (request["completion_token"] + ".json")
    if path.resolve() != expected.resolve():
        raise session_close.Refusal("close request is outside canonical state")
    return request


def require_current(request, path, digest):
    if load_request(path, digest) != request or session_close.read_json(canonical_path(request)) != request:
        raise session_close.Cancelled("close request generation changed")
    if Path(request["cancel"]).exists():
        raise session_close.Cancelled("close request was cancelled")
    import close_lifecycle
    from host import Host

    try:
        close_lifecycle.require_input(Host(request["host"]), request["session_id"], request)
    except ValueError as error:
        raise session_close.Cancelled(str(error)) from error
    assessment = session_close.read_json(close_lifecycle.state_root() / "assessments" / canonical_path(request).name)
    if (not isinstance(assessment, dict) or assessment.get("status") != "armed"
            or assessment.get("decision") not in ("complete", "restart")
            or assessment.get("assessment_generation") != request.get("assessment_generation")
            or assessment.get("input_generation") != request.get("input_generation")
            or assessment.get("completion_token") != request["completion_token"]):
        raise session_close.Cancelled("the close assessment changed")


def complete(request):
    value = session_close.read_json(request["completion"])
    return isinstance(value, dict) and value.get("token") == request["completion_token"]


def final_local_guard(request, path, digest, activity):
    require_current(request, path, digest)
    entry = request["entry"]
    current = session_close.read_json(request["registry"])
    fields = (*session_close.CLIENT_BINDING_FIELDS, "cwd", "frontend", "provenance", "terminal")
    if not isinstance(current, dict) or any(current.get(key) != entry.get(key) for key in fields):
        raise session_close.Cancelled("session registration changed before close")
    session_close.host_sessions(request["host"]).close_guard(request, activity, complete(request), session_close.SHARED)


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
                    def guard():
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
