"""Detached, identity-checked observer for peer-cli close (Python 3.9+)."""
import argparse
import os
import time
from pathlib import Path
import session_close


def arguments():
    parser = argparse.ArgumentParser()
    values = (
        ("host-pid", int, True), ("host-start", float, True),
        ("worktree", str, True), ("result", str, True),
        ("session-id", str, True), ("invocation-id", str, True),
        ("parent-pid", int, False), ("parent-start", float, False),
        ("spawn-timeout", float, False), ("armed-timeout", float, False),
        ("accept-timeout", float, False), ("reaper-timeout", float, False),
        ("state-directory", str, False), ("host-names", str, False),
    )
    for name, kind, required in values:
        parser.add_argument("--" + name, type=kind, required=required)
    return parser.parse_args()


def bounded_timeout(value, name):
    if value is None:
        return session_close.bounded_timeout(name)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return session_close.DEFAULT_TIMEOUTS[name]
    if (not session_close.math.isfinite(parsed) or parsed <= 0
            or parsed > session_close.MAX_TIMEOUTS[name]):
        return session_close.DEFAULT_TIMEOUTS[name]
    return parsed


def result(path, status, **extra):
    value = session_close.read_json(path) or {}
    value.update({"status": status, "finished": session_close.now()})
    value.update(extra)
    session_close.write_json(path, value)


def await_acceptance(path, observer, expected, timeout):
    end = session_close.now() + timeout
    while session_close.now() < end:
        if Path(str(path) + ".cancelled").exists():
            return False
        value = session_close.read_json(str(path) + ".accepted")
        if value and session_close.valid_acknowledgement(value, expected, observer):
            session_close.write_json(
                str(path) + ".armed",
                {**expected, "armed": session_close.now(),
                 "reaper_pid": observer["reaper_pid"],
                 "reaper_started_at": observer["reaper_started_at"],
                 "observer_pid": observer["reaper_pid"],
                 "observer_started_at": observer["reaper_started_at"]},
            )
            return True
        time.sleep(.05)
    return False


def await_exit(args):
    end = session_close.now() + bounded_timeout(args.reaper_timeout, "AGENT_FINISH_REAPER_TIMEOUT_SECONDS")
    watched = [(args.host_pid, args.host_start)]
    if os.name == "nt" and args.parent_pid and args.parent_start:
        watched.append((args.parent_pid, args.parent_start))
    while session_close.now() < end:
        if Path(str(args.result) + ".cancelled").exists():
            return False
        if all(session_close.verified_exited(pid, started) for pid, started in watched):
            return True
        time.sleep(.25)
    return False


def main():
    args = arguments()
    if args.state_directory:
        os.environ["AGENT_STATE_DIRECTORY"] = args.state_directory
    if args.host_names:
        os.environ["AGENT_CLI_HOST_NAMES"] = args.host_names
    target = Path(args.result)
    own = session_close.process_info(os.getpid())
    if own is None:
        return 0
    expected = session_close.binding(
        args.host_pid, args.host_start, args.worktree, args.session_id, args.invocation_id,
    )
    if args.parent_pid is not None or args.parent_start is not None:
        expected.update({"parent_pid": args.parent_pid, "parent_start": args.parent_start})
    observer = {
        **expected,
        "started": session_close.now(),
        "reaper_pid": own.pid,
        "reaper_started_at": own.started_at,
        "observer_pid": own.pid,
        "observer_started_at": own.started_at,
    }
    if not session_close.write_json(target, observer, exclusive=True):
        return 0
    accept_timeout = bounded_timeout(args.accept_timeout, "AGENT_FINISH_ACCEPT_TIMEOUT_SECONDS")
    if not await_acceptance(target, observer, expected, accept_timeout):
        result(target, "cancelled" if Path(str(target) + ".cancelled").exists() else "not-accepted")
        return 0
    if not await_exit(args):
        status = "cancelled" if Path(str(target) + ".cancelled").exists() else "timeout"
        result(target, status, error="verified host (or Windows wrapper) did not exit")
        return 0
    session_close.delete_own_session(args.session_id)
    session_close.remove_matching_obligations(args.worktree, args.session_id)
    result(target, "session-closed", checkout_retained=True, session_exited=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
