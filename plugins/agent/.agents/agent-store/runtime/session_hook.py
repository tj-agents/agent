#!/usr/bin/env python3
"""Record host session lifecycle hook events in the session store (Python 3.9+)."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tj_agent_store  # noqa: E402

EVENTS = ("session-start", "prompt", "stop", "session-end")
HOSTS = ("claude", "codex")


def _read_json_stdin():
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    try:
        value = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _record(event, host, session_id, cwd, payload):
    handle = tj_agent_store.session_handle(host, session_id)
    with tj_agent_store.SessionStore() as store:
        if event == "session-start":
            store.start_session(host, session_id, cwd, tj_agent_store.Checkout.probe(cwd))
        elif event == "prompt":
            store.update_session(handle, lambda session: session.prompted(payload.get("prompt")))
        elif event == "stop":
            checkout = tj_agent_store.Checkout.probe(cwd)
            registry = tj_agent_store.CliSessionRegistry(tj_agent_store.state_directory() / "cli-sessions")
            if registry.follow_cwd(host, session_id, checkout.root) is False:
                checkout = None
            store.record_stop(handle, checkout)
        elif event == "session-end":
            store.update_session(handle, lambda session: session.end("closed"))


def run_hook(event, host):
    try:
        payload = _read_json_stdin()
        session_id = payload.get("session_id") or payload.get("sessionId")
        if isinstance(session_id, str) and session_id:
            _record(event, host, session_id, payload.get("cwd") or os.getcwd(), payload)
    except Exception:
        pass
    return 0


def _build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", choices=EVENTS)
    parser.add_argument("--host", required=True, choices=HOSTS)
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    return run_hook(args.event, args.host)


if __name__ == "__main__":
    sys.exit(main())
