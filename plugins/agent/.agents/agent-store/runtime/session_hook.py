#!/usr/bin/env python3
"""Record host session lifecycle hook events in the session store (Python 3.9+)."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

_LOADER_PATH = Path(__file__).resolve().with_name("agent_store_loader.py")
_LOADER_KEY = f"agent_store_loader_{hashlib.sha256(str(_LOADER_PATH).encode('utf-8')).hexdigest()[:16]}"
if _LOADER_KEY not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_LOADER_KEY, _LOADER_PATH)
    _loader = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_loader)
    sys.modules[_LOADER_KEY] = _loader
load_sibling = sys.modules[_LOADER_KEY].load_sibling

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
    store_module = load_sibling(__file__, "session_store")
    handle = store_module.session_handle(host, session_id)
    with store_module.SessionStore() as store:
        if event == "session-start":
            store.start_session(host, session_id, cwd, store_module.Checkout.probe(cwd))
        elif event == "prompt":
            store.update_session(handle, lambda session: session.prompted(payload.get("prompt")))
        elif event == "stop":
            checkout = store_module.Checkout.probe(cwd)
            registry = load_sibling(__file__, "cli_session_registry").CliSessionRegistry(
                store_module.state_directory() / "cli-sessions",
            )
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
