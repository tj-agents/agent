import importlib.util
import json
import os
import shutil
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

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
            processes.require_live(identity)

        if processes.matches(identity):
            try:
                processes.stop(identity, before_signal=authorize_stop)
            except processes.Refusal:
                if processes.matches(identity):
                    raise


@pytest.mark.parametrize(
    "name", ["child-started.json", "started.json", "frontend.json"]
)
@pytest.mark.parametrize("phase", ["handle", "authorization"])
def test_fixture_cleanup_accepts_exact_process_exit_during_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, phase: str
) -> None:
    from tj_cli import processes

    identity = {"pid": 123, "started_at": 456.0}
    (tmp_path / name).write_text(json.dumps(identity), encoding="utf-8")
    refusal = processes.Refusal("exact fixture process exited")
    matches = Mock(side_effect=[True, False])
    require_live = Mock(side_effect=refusal)

    def stop(_identity: object, *, before_signal: Callable[[], None]) -> None:
        if phase == "authorization":
            before_signal()
        raise refusal

    stop_process = Mock(side_effect=stop)
    monkeypatch.setattr(processes, "matches", matches)
    monkeypatch.setattr(processes, "require_live", require_live)
    monkeypatch.setattr(processes, "stop", stop_process)

    cleanup_fixture_processes(tmp_path)

    assert matches.call_args_list == [call(identity), call(identity)]
    stop_process.assert_called_once()
    assert stop_process.call_args.args == (identity,)
    if phase == "authorization":
        require_live.assert_called_once_with(identity)
    else:
        require_live.assert_not_called()


@pytest.mark.parametrize(
    "name", ["child-started.json", "started.json", "frontend.json"]
)
def test_fixture_cleanup_preserves_refusal_for_matching_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from tj_cli import processes

    identity = {"pid": 123, "started_at": 456.0}
    (tmp_path / name).write_text(json.dumps(identity), encoding="utf-8")
    refusal = processes.Refusal("cannot acquire exact frontend process handle")
    matches = Mock(return_value=True)
    stop = Mock(side_effect=refusal)
    monkeypatch.setattr(processes, "matches", matches)
    monkeypatch.setattr(processes, "stop", stop)

    with pytest.raises(processes.Refusal) as caught:
        cleanup_fixture_processes(tmp_path)

    assert caught.value is refusal
    assert matches.call_args_list == [call(identity), call(identity)]
    stop.assert_called_once()
    assert stop.call_args.args == (identity,)


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
