import importlib.util
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/agent/.agents/agent/utility/peer/scripts"
FIXTURE = Path(__file__).parent / "fixtures/hidden_observer.py"


def new_terminal_windows(
    before: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    known = {(entry["handle"], entry["pid"], entry["class"]) for entry in before}
    return [
        entry
        for entry in current
        if (entry["handle"], entry["pid"], entry["class"]) not in known
        and (
            entry["class"] == "ConsoleWindowClass"
            or "CASCADIA" in entry["class"].upper()
        )
    ]


def test_terminal_detection_includes_windows_owned_by_a_different_process() -> None:
    original = {"handle": 1, "pid": 101, "class": "ConsoleWindowClass"}
    terminal = {"handle": 2, "pid": 202, "class": "CASCADIA_HOSTING_WINDOW_CLASS"}
    assert new_terminal_windows([original], [original, terminal]) == [terminal]


def cleanup_fixture_processes(state: Path) -> None:
    from tj_cli import processes

    for name in ("child-started.json", "started.json", "frontend.json"):
        path = state / name
        if not path.exists():
            continue
        identity = json.loads(path.read_text(encoding="utf-8"))

        def authorize_stop(identity: dict[str, Any] = identity) -> None:
            assert processes.matches(identity)

        if processes.matches(identity):
            processes.stop(identity, before_signal=authorize_stop)


@pytest.mark.skipif(
    shutil.which("tj_hook") is None, reason="installed native tj_hook is required"
)
def test_detached_observer_and_descendant_remain_hidden_after_frontend_exit(
    tmp_path: Path,
) -> None:
    from tj_cli.process import run

    spec = importlib.util.spec_from_file_location("hidden_observer_fixture", FIXTURE)
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    state = tmp_path / "observer state é"
    state.mkdir()
    before = fixture.visible_windows()

    def observe_windows() -> None:
        assert not new_terminal_windows(before, fixture.visible_windows()), (
            "new visible terminal window makes hidden-launch verification inconclusive"
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            frontend_run = executor.submit(
                run,
                [
                    sys.executable,
                    "-X",
                    "utf8",
                    str(FIXTURE),
                    "frontend",
                    str(SCRIPTS),
                    str(state),
                ],
                capture_output=True,
                timeout=45,
            )
            while not frontend_run.done():
                observe_windows()
                time.sleep(0.05)
            frontend = frontend_run.result()
        assert frontend.returncode == 0, frontend.stderr.decode(
            "utf-8", errors="replace"
        )
        deadline = time.monotonic() + 20
        while not (state / "finished.json").exists():
            observe_windows()
            assert not (state / "failure.json").exists(), (
                state / "failure.json"
            ).read_text(encoding="utf-8")
            assert time.monotonic() < deadline, (
                "detached observer did not survive the dummy frontend"
            )
            time.sleep(0.05)
        observe_windows()
    finally:
        cleanup_fixture_processes(state)
    result = json.loads((state / "finished.json").read_text(encoding="utf-8"))
    child = json.loads((state / "child.json").read_text(encoding="utf-8"))
    assert result["survived_frontend"] is True
    assert result["child_returncode"] == 0
    observations = result["observations"] + child["observations"]
    assert observations
    assert all(not item["visible"] and not item["windows"] for item in observations)
    if os.name == "nt":
        assert all(item["console"] for item in observations)
