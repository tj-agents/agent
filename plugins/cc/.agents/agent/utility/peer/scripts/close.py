"""Close this verified session while retaining its checkout (Python 3.9+)."""
import argparse
import os
import sys
import time
import uuid
import close_tab
import session_close
import register_session
from session_close import _record_own_end, _reopen_own_end


def close_host(entry, wrapper=None):
    if not session_close.verified_live(entry["pid"], entry["pid_started_at"]):
        raise session_close.Refusal("registered host identity changed before close")
    close_tab.close_entry(entry)


def refresh_attachment(entry, worktree):
    refreshed = session_close.own_host_and_entry()
    if (refreshed["session_id"] != entry["session_id"]
            or refreshed["pid"] != entry["pid"]
            or refreshed["pid_started_at"] != entry["pid_started_at"]
            or session_close.path_key(worktree) != session_close.path_key(session_close.worktree_from_cwd())
            or not session_close.attached_to(os.getcwd(), refreshed)):
        raise session_close.Refusal("session attachment changed while observer started")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    entry = session_close.own_host_and_entry()
    try:
        return close_session(entry)
    except (session_close.Refusal, close_tab.TerminalRefusal) as error:
        session_close.record_close_refusal(entry, __file__, list(argv) if argv is not None else sys.argv[1:], error)
        raise


def close_session(entry):
    worktree = session_close.worktree_from_cwd()
    if not session_close.attached_to(os.getcwd(), entry):
        raise session_close.Refusal("current directory is not this session's registered attachment")
    activity = session_close.awaiting_input_activity(entry)
    result = session_close.results_path()
    invocation_id = uuid.uuid4().hex
    wrapper = session_close.qualified_windows_wrapper(entry)
    expected = session_close.binding(
        entry["pid"], entry["pid_started_at"], worktree, entry["session_id"], invocation_id,
    )
    if wrapper:
        expected.update({"parent_pid": wrapper["pid"], "parent_start": wrapper["started_at"]})
    observer = [
        "--host-pid", str(entry["pid"]), "--host-start", str(entry["pid_started_at"]),
        "--worktree", str(worktree), "--result", str(result), "--session-id", entry["session_id"],
        "--invocation-id", invocation_id,
        "--state-directory", str(session_close.state_directory()),
        "--host-names", os.environ.get("AGENT_CLI_HOST_NAMES", ""),
        "--spawn-timeout", str(session_close.bounded_timeout("AGENT_FINISH_SPAWN_TIMEOUT_SECONDS")),
        "--armed-timeout", str(session_close.bounded_timeout("AGENT_FINISH_ARMED_TIMEOUT_SECONDS")),
        "--accept-timeout", str(session_close.bounded_timeout("AGENT_FINISH_ACCEPT_TIMEOUT_SECONDS")),
        "--reaper-timeout", str(session_close.bounded_timeout("AGENT_FINISH_REAPER_TIMEOUT_SECONDS")),
    ]
    if wrapper:
        observer.extend(["--parent-pid", str(wrapper["pid"]),
                         "--parent-start", str(wrapper["started_at"])])
    recorded_end = False
    try:
        spawned_at = time.time()
        observer_pid = session_close.start_observer(observer)
        started = session_close.wait_for_record(
            result, session_close.bounded_timeout("AGENT_FINISH_SPAWN_TIMEOUT_SECONDS"),
        )
        if not session_close.valid_observer_start(
                started, observer_pid, entry["pid"], entry["pid_started_at"], worktree,
                entry["session_id"], invocation_id, minimum_started=spawned_at,
        ):
            raise session_close.Refusal("session exit observer did not confirm startup")
        refresh_attachment(entry, worktree)
        accepted = {
            **expected,
            "accepted": time.time(),
            "reaper_pid": started["reaper_pid"],
            "reaper_started_at": started["reaper_started_at"],
            "observer_pid": started["reaper_pid"],
            "observer_started_at": started["reaper_started_at"],
        }
        session_close.write_json(str(result) + ".accepted", accepted)
        armed = session_close.wait_for_record(
            str(result) + ".armed", session_close.bounded_timeout("AGENT_FINISH_ARMED_TIMEOUT_SECONDS"),
        )
        if not session_close.valid_armed(armed, expected, started, accepted):
            raise session_close.Refusal("session exit observer did not acknowledge acceptance")
        session_close.awaiting_input_activity(entry, activity)
        _record_own_end(entry, "closed")
        recorded_end = True
        close_host(entry, wrapper)
    except Exception as error:
        if recorded_end:
            _reopen_own_end(entry, "closed")
        session_close.cancel_observer(result, str(error))
        raise
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (session_close.Refusal, close_tab.TerminalRefusal) as error:
        print("close: " + str(error), file=sys.stderr)
        raise SystemExit(2)
