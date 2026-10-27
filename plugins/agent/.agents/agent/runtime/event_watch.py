from __future__ import annotations

import json
import math
from pathlib import Path
import time
import urllib.request

import launch_check
import machine_events
import plugin_autoupdate as plugins


def stream(topic: str, since: str, open_url=urllib.request.urlopen):
    machine_events.topic_value(topic)
    if since != "all" and not launch_check.MESSAGE_ID.fullmatch(since):
        raise ValueError("Invalid machine-event cursor")
    request = urllib.request.Request(f"https://{launch_check.SERVER}/{topic}/json?since={since}",
                                     headers={"User-Agent": "tj-agents-machine-events"})
    with open_url(request, timeout=70) as response:
        while line := response.readline(launch_check.MAX_RESPONSE + 1):
            if len(line) > launch_check.MAX_RESPONSE:
                raise ValueError("Machine-event message exceeds the size limit")
            try:
                envelope = json.loads(line)
            except ValueError:
                continue
            if (isinstance(envelope, dict) and envelope.get("event") == "message"
                    and isinstance(envelope.get("id"), str) and launch_check.MESSAGE_ID.fullmatch(envelope["id"])):
                yield envelope


def consume(directory: Path, envelopes=(), *, catch_up=False, api=machine_events.gh, now=time.time,
            factory=machine_events.Reconciler):
    lock = plugins.acquire_lock(directory / "machine-events.lock")
    if lock is None:
        raise RuntimeError("Machine-event reconciliation is already running")
    try:
        state_path = directory / "machine-events-state.json"
        cursor_path = directory / "machine-events-watch-cursor.json"
        reconciler = factory(plugins.read_json(state_path))
        cursor = plugins.read_json(cursor_path)
        try:
            if catch_up and reconciler.catch_up(machine_events.catch_up_states(api=api)):
                raise RuntimeError("Machine-event catch-up left unresolved repositories")
            for envelope in envelopes:
                repository = machine_events.parse_hint(envelope)
                if repository:
                    reconciler.repository(machine_events.repository_state(repository, api=api))
        finally:
            plugins.write_json(state_path, reconciler.state)
        last = envelopes[-1]["id"] if envelopes else cursor.get("since", "all")
        plugins.write_json(cursor_path, {"since": last, "checked_at": now()})
    finally:
        lock.close()


def run(directory: Path, *, events=stream, sleep=time.sleep, now=time.time):
    topic = machine_events.topic_value(plugins.read_json(directory / "machine-events.json").get("topic"))
    receipt = directory / "bin/source.json"
    installed = receipt.read_bytes() if receipt.is_file() else None
    while True:
        try:
            cursor = plugins.read_json(directory / "machine-events-watch-cursor.json")
            checked = cursor.get("checked_at")
            if (not isinstance(checked, (int, float)) or not math.isfinite(checked)
                    or now() - checked >= launch_check.EXPIRY_SECONDS):
                consume(directory, catch_up=True, now=now)
                cursor = plugins.read_json(directory / "machine-events-watch-cursor.json")
                if installed != (receipt.read_bytes() if receipt.is_file() else None):
                    return 1
            for envelope in events(topic, cursor.get("since", "all")):
                consume(directory, [envelope], now=now)
                current = receipt.read_bytes() if receipt.is_file() else None
                if installed != current:
                    machine_events.LOG.info("Installed Agent runtime changed; supervisor restart required")
                    return 1
            sleep(1)
        except (OSError, RuntimeError, ValueError) as error:
            machine_events.LOG.warning("Machine-event subscriber will retry: %s", error)
            sleep(30)
