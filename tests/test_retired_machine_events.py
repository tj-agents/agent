import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))

import retired_machine_events  # noqa: E402

AUTHORED = [ROOT / ".agents", ROOT / ".claude" / "runtime", ROOT / ".codex" / "runtime", ROOT / "install.ps1",
            ROOT / "install.sh"]
CREATORS = ("machine_events.py", "--watch", "Register-ScheduledTask", "schtasks /Create", "\"/Create\"",
            "systemctl --user enable", "\"enable\"")


class Recorder:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, self.returncode, "", "")


def task_name(state: Path, windows: bool) -> str:
    text = str(state.resolve())
    digest = hashlib.sha256((text.casefold() if windows else text).encode()).hexdigest()[:16]
    return f"tj-agents-machine-events-{digest}"


def stale_runtime(state: Path) -> Path:
    runtime = state / "bin" / ".agents" / "agent" / "runtime"
    runtime.mkdir(parents=True)
    for name in (*retired_machine_events.RUNTIME_FILES, "install.py"):
        (runtime / name).write_text("", encoding="utf-8")
    return runtime


def test_windows_deletes_the_task_and_the_stale_runtime_files(tmp_path: Path) -> None:
    runtime = stale_runtime(tmp_path)
    run = Recorder()
    removed = retired_machine_events.remove(tmp_path, windows=True, run=run)
    assert run.commands == [["schtasks", "/Delete", "/TN", task_name(tmp_path, True), "/F"]]
    assert f"scheduled task {task_name(tmp_path, True)}" in removed
    assert sorted(path.name for path in runtime.iterdir()) == ["install.py"]


def test_linux_disables_and_deletes_the_user_unit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    unit = home / ".config" / "systemd" / "user" / f"{task_name(tmp_path, False)}.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n", encoding="utf-8")
    run = Recorder()
    removed = retired_machine_events.remove(tmp_path, windows=False, home=home, run=run)
    assert run.commands == [["systemctl", "--user", "disable", "--now", unit.name],
                            ["systemctl", "--user", "daemon-reload"]]
    assert not unit.exists()
    assert removed == [f"systemd user unit {unit.name}"]


@pytest.mark.parametrize("windows", [True, False])
def test_nothing_is_reported_once_the_watcher_is_gone(tmp_path: Path, windows: bool) -> None:
    run = Recorder(returncode=1)
    assert retired_machine_events.remove(tmp_path, windows=windows, home=tmp_path / "home", run=run) == []
    assert run.commands == ([["schtasks", "/Delete", "/TN", task_name(tmp_path, True), "/F"]] if windows else [])


def test_a_missing_process_launcher_never_breaks_the_caller(tmp_path: Path) -> None:
    runtime = stale_runtime(tmp_path)

    def missing(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("tj_hook is not installed on PATH")

    assert retired_machine_events.remove(tmp_path, windows=True, run=missing) == [
        str(runtime / name) for name in retired_machine_events.RUNTIME_FILES]


def test_no_authored_code_can_create_the_watcher_again() -> None:
    allowed = {ROOT / ".agents" / "agent" / "runtime" / "retired_machine_events.py"}
    offenders = []
    for root in AUTHORED:
        files = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
        for path in files:
            if path in allowed or path.suffix not in {".py", ".ps1", ".sh", ".json", ".toml"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            offenders += [f"{path.relative_to(ROOT)}: {term}" for term in CREATORS if term in text]
    assert offenders == []
