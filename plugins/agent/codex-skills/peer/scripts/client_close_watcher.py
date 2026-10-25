"""Wait for one Agent close request then stop its frozen frontend (Python 3.9+)."""

import argparse
import json
from pathlib import Path
import time

import session_close


def _read(path):
    return session_close.read_json(path)


def _write(path, value):
    session_close.write_json(path, value)


def _cancelled(path):
    return _read(path) is not None


def _complete(path, token):
    value = _read(path)
    return isinstance(value, dict) and value.get("token") == token


def _record_end(entry):
    store = session_close.register_session._load_session_store()
    if store is None:
        raise session_close.Refusal("session store is unavailable")
    connection = store.connect()
    try:
        store.end_session(connection, store.session_handle(entry["host"], entry["session_id"]), "closed")
    finally:
        connection.close()


def wait(entry, completion, cancel, token, result, timeout, cli_root):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _cancelled(cancel):
            _write(result, {"status": "cancelled"})
            return 0
        if _complete(completion, token):
            def guard():
                if _cancelled(cancel) or not _complete(completion, token):
                    raise session_close.Refusal("close request was cancelled before the frontend signal")
                session_close.awaiting_input_activity(entry, entry["expected_activity"])

            try:
                session_close.stop_cli_client(entry, guard, cli_root)
                _record_end(entry)
            except session_close.Refusal as error:
                _write(result, {"status": "refused", "reason": str(error)})
                return 2
            _write(result, {"status": "closed", "session_id": entry["session_id"]})
            return 0
        time.sleep(0.05)
    _write(result, {"status": "timeout"})
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--completion", type=Path, required=True)
    parser.add_argument("--cancel", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)
    request = _read(args.request)
    if not isinstance(request, dict):
        return 2
    entry, token, cli_root = request.get("entry"), request.get("completion_token"), request.get("cli_root")
    if not isinstance(entry, dict) or not isinstance(token, str) or not token or not isinstance(cli_root, str):
        return 2
    timeout = args.timeout if session_close.finite_positive(args.timeout) else 600.0
    return wait(entry, args.completion, args.cancel, token, args.result, timeout, cli_root)


if __name__ == "__main__":
    raise SystemExit(main())
