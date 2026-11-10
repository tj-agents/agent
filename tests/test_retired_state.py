import hashlib
import inspect
import io
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))

import install  # noqa: E402
import retired_state  # noqa: E402

AUTHORED = [ROOT / ".agents", ROOT / ".claude" / "runtime", ROOT / ".codex" / "runtime"]
TEXT_SUFFIXES = {".py", ".ps1", ".sh", ".json", ".toml"}
LIVE_ENTRIES = (
    "bin/source.json",
    "bin/bot-identity/bot_identity.py",
    "bin/.agents/agent/runtime/agent_cli.py",
    "plugin-refresh.json",
    "github-access/profile.json",
)


@pytest.fixture(autouse=True)
def isolated_machine(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = tmp_path_factory.mktemp("machine")
    monkeypatch.setenv("APPDATA", str(machine / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(machine / "local"))


class Recorder:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.commands: list[list[str]] = []

    def __call__(
        self, command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, self.returncode, "", "")


def task_name(state: Path, windows: bool) -> str:
    text = str(state.resolve())
    digest = hashlib.sha256(
        (text.casefold() if windows else text).encode()
    ).hexdigest()[:16]
    return f"tj-agents-machine-events-{digest}"


DIR_ENTRIES_WITH_A_DOTTED_NAME = {"published-repository.git"}


def seed(state: Path) -> None:
    for entry in retired_state.RETIRED_ENTRIES:
        path = state / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        if entry in DIR_ENTRIES_WITH_A_DOTTED_NAME or "." not in Path(entry).name:
            path.mkdir()
        else:
            path.write_text("", encoding="utf-8")
    for entry in LIVE_ENTRIES:
        path = state / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


def test_every_retired_entry_is_removed(tmp_path: Path) -> None:
    seed(tmp_path)
    removed = retired_state.remove(tmp_path, windows=True, run=Recorder()).removed
    for entry in retired_state.RETIRED_ENTRIES:
        assert not (tmp_path / entry).exists(), entry
    assert len(removed) >= len(retired_state.RETIRED_ENTRIES)


def test_every_live_entry_survives(tmp_path: Path) -> None:
    seed(tmp_path)
    retired_state.remove(tmp_path, windows=True, run=Recorder())
    for entry in LIVE_ENTRIES:
        assert (tmp_path / entry).is_file(), entry


def test_cli_sessions_and_sessions_db_are_kept(tmp_path: Path) -> None:
    kept = tmp_path / "cli-sessions"
    kept.mkdir()
    (kept / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "sessions.db").write_text("x", encoding="utf-8")
    retired_state.remove(tmp_path, windows=True, run=Recorder())
    assert (kept / "a.json").exists()
    assert (tmp_path / "sessions.db").exists()


def test_a_link_pointing_outside_the_state_directory_is_not_followed(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "keep.json"
    target.write_text("precious", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    link = state / "codex-plugin-bytes.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this filesystem or account does not allow creating a symlink")
    retired_state.remove(state, windows=True, run=Recorder())
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "precious"
    assert not link.exists()


def test_windows_deletes_the_scheduled_task(tmp_path: Path) -> None:
    run = Recorder()
    removed = retired_state.remove(tmp_path, windows=True, run=run).removed
    assert [task_name(tmp_path, True)] == [
        cmd[3] for cmd in run.commands if cmd[0] == "schtasks"
    ]
    assert f"scheduled task {task_name(tmp_path, True)}" in removed


def test_linux_disables_and_deletes_the_user_unit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    unit = (
        home / ".config" / "systemd" / "user" / f"{task_name(tmp_path, False)}.service"
    )
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n", encoding="utf-8")
    run = Recorder()
    removed = retired_state.remove(tmp_path, windows=False, home=home, run=run).removed
    assert run.commands == [
        ["systemctl", "--user", "disable", "--now", unit.name],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert not unit.exists()
    assert f"systemd user unit {unit.name}" in removed


@pytest.mark.parametrize("windows", [True, False])
def test_nothing_is_reported_once_the_watcher_is_gone(
    tmp_path: Path, windows: bool
) -> None:
    run = Recorder(returncode=1)
    removed = retired_state.remove(
        tmp_path, windows=windows, home=tmp_path / "home", run=run
    ).removed
    assert removed == []


def test_a_missing_process_launcher_never_breaks_the_caller(tmp_path: Path) -> None:
    seed(tmp_path)

    def missing(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("tj_hook is not installed on PATH")

    removed = retired_state.remove(tmp_path, windows=True, run=missing).removed
    for entry in retired_state.RETIRED_ENTRIES:
        assert not (tmp_path / entry).exists(), entry
    assert removed == [str(tmp_path / entry) for entry in retired_state.RETIRED_ENTRIES]


def test_no_authored_code_references_a_retired_name_as_a_path() -> None:
    allowed = {ROOT / ".agents" / "agent" / "runtime" / "retired_state.py"}
    offenders = []
    for root in AUTHORED:
        if not root.exists():
            continue
        files = (
            [root]
            if root.is_file()
            else [path for path in root.rglob("*") if path.is_file()]
        )
        for path in files:
            if path in allowed or path.suffix not in TEXT_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for entry in retired_state.RETIRED_ENTRIES:
                if entry in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {entry}")
    assert offenders == []


class FakeProcesses:
    def __init__(self, running: list[retired_state.RunningProcess]) -> None:
        self.running = running
        self.terminated: list[retired_state.RunningProcess] = []

    def listing(self) -> list[retired_state.RunningProcess]:
        return list(self.running)

    def terminate(self, running: retired_state.RunningProcess) -> bool:
        self.terminated.append(running)
        return True


def seed_tj_watch(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    environ = {
        "APPDATA": str(tmp_path / "roaming"),
        "LOCALAPPDATA": str(tmp_path / "local"),
    }
    shortcut = tmp_path / "roaming" / retired_state.TJ_WATCH_SHORTCUT
    binary = tmp_path / "local" / retired_state.TJ_WATCH_EXECUTABLE
    for path in (shortcut, binary):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"tj-watch")
    return environ, shortcut, binary


def test_windows_retires_the_tj_watch_shortcut_binary_and_process(
    tmp_path: Path,
) -> None:
    environ, shortcut, binary = seed_tj_watch(tmp_path)
    running = retired_state.RunningProcess(41, str(binary))
    processes = FakeProcesses([running])
    removed = retired_state.remove(
        tmp_path / "state",
        windows=True,
        run=Recorder(),
        environ=environ,
        processes=processes,
    ).removed
    assert processes.terminated == [running]
    assert not shortcut.exists()
    assert not binary.exists()
    assert [str(shortcut), "tj-watch process 41", str(binary)] == removed[-3:]


def test_tj_watch_retirement_never_stops_a_process_by_name(tmp_path: Path) -> None:
    environ, _shortcut, binary = seed_tj_watch(tmp_path)
    exact = retired_state.RunningProcess(41, str(binary))
    elsewhere = retired_state.RunningProcess(
        42, str(tmp_path / "elsewhere" / "tj-watch.exe")
    )
    named = retired_state.RunningProcess(43, "tj-watch.exe")
    processes = FakeProcesses([elsewhere, named, exact])
    retired_state.remove(
        tmp_path / "state",
        windows=True,
        run=Recorder(),
        environ=environ,
        processes=processes,
    )
    assert processes.terminated == [exact]
    source = inspect.getsource(retired_state)
    assert "QueryFullProcessImageNameW" in source
    for by_name in ("szExeFile", "taskkill", "/IM", "Get-Process", "Win32_Process"):
        assert by_name not in source


def test_a_locked_tj_watch_binary_fails_the_install_and_other_cleanup_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ, shortcut, binary = seed_tj_watch(tmp_path)
    context = install.Context(
        home=tmp_path, environ=environ, windows=True, run=Recorder()
    )
    seed(context.state_directory)
    unlink = Path.unlink

    def deny_binary(path: Path, missing_ok: bool = False) -> None:
        if path == binary:
            raise PermissionError(13, "Access is denied", str(path))
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny_binary)
    monkeypatch.setattr(retired_state, "WindowsProcessTable", lambda: FakeProcesses([]))
    removal = retired_state.remove(
        context.state_directory,
        windows=True,
        run=Recorder(),
        environ=environ,
        processes=FakeProcesses([]),
    )
    assert binary.is_file()
    assert not shortcut.exists()
    assert str(shortcut) in removal.removed
    assert len(removal.failures) == 1
    assert str(binary) in removal.failures[0]
    assert all(
        not (context.state_directory / entry).exists()
        for entry in retired_state.RETIRED_ENTRIES
    )
    step = next(item for item in install.STEPS if item.name == "retired state")
    errors = io.StringIO()
    assert install.run_steps([step], context, False, io.StringIO(), errors) == 2
    assert str(binary) in errors.getvalue()


def test_tj_watch_is_left_alone_off_windows(tmp_path: Path) -> None:
    environ, shortcut, binary = seed_tj_watch(tmp_path)
    processes = FakeProcesses([retired_state.RunningProcess(41, str(binary))])
    retired_state.remove(
        tmp_path / "state",
        windows=False,
        home=tmp_path / "home",
        run=Recorder(),
        environ=environ,
        processes=processes,
    )
    assert processes.terminated == []
    assert shortcut.exists()
    assert binary.exists()


def test_present_names_what_remove_retires_without_removing_it(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    seed(state)
    environ, shortcut, binary = seed_tj_watch(tmp_path)
    run = Recorder()
    found = retired_state.present(state, windows=True, run=run, environ=environ)
    assert found == [
        f"scheduled task {task_name(state, True)}",
        str(shortcut),
        str(binary),
        *(str(state / entry) for entry in retired_state.RETIRED_ENTRIES),
    ]
    assert run.commands == [["schtasks", "/Query", "/TN", task_name(state, True)]]
    assert all((state / entry).exists() for entry in retired_state.RETIRED_ENTRIES)
    retired_state.remove(
        state,
        windows=True,
        run=Recorder(),
        environ=environ,
        processes=FakeProcesses([]),
    )
    gone = Recorder(returncode=1)
    assert retired_state.present(state, windows=True, run=gone, environ=environ) == []


def test_the_retired_state_install_step_runs_alone_and_converges(
    tmp_path: Path,
) -> None:
    context = install.Context(home=tmp_path, environ={}, windows=False, run=Recorder())
    seed(context.state_directory)
    step = next(item for item in install.STEPS if item.name == "retired state")
    output = io.StringIO()
    assert install.run_steps([step], context, True, output, io.StringIO()) == 1
    assert "drift  retired state: remove " in output.getvalue()
    assert install.run_steps([step], context, False, io.StringIO(), io.StringIO()) == 0
    assert step.check(context) == []
    for entry in LIVE_ENTRIES:
        assert (context.state_directory / entry).is_file(), entry


def test_a_read_only_file_inside_a_retired_directory_is_removed(
    tmp_path: Path,
) -> None:
    objects = tmp_path / "published-repository.git" / "objects" / "00"
    objects.mkdir(parents=True)
    packed = objects / "8e3d"
    packed.write_bytes(b"blob")
    packed.chmod(stat.S_IREAD)
    loose = tmp_path / "plugin-heads.json"
    loose.write_text("{}", encoding="utf-8")
    loose.chmod(stat.S_IREAD)
    removal = retired_state.remove(tmp_path, windows=True, run=Recorder())
    assert removal.failures == []
    assert not (tmp_path / "published-repository.git").exists()
    assert not loose.exists()


def test_a_removal_that_still_fails_is_reported_and_the_rest_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(tmp_path)
    stuck = tmp_path / "published-repository.git"
    rmtree = shutil.rmtree

    def refuse(path: Path, **options: Any) -> None:
        if Path(path).name == stuck.name:
            raise PermissionError(13, "Access is denied", str(path))
        rmtree(path, **options)

    monkeypatch.setattr(shutil, "rmtree", refuse)
    removal = retired_state.remove(tmp_path, windows=True, run=Recorder())
    assert len(removal.failures) == 1
    assert removal.failures[0].startswith(f"could not remove {stuck}: ")
    assert stuck.exists()
    for entry in retired_state.RETIRED_ENTRIES:
        if entry != stuck.name:
            assert not (tmp_path / entry).exists(), entry

    context = install.Context(home=tmp_path, environ={}, windows=False, run=Recorder())
    seed(context.state_directory)
    later = install.Step("later", lambda _context: ["pending"], lambda _context: [])
    retired = next(item for item in install.STEPS if item.name == "retired state")
    output, errors = io.StringIO(), io.StringIO()
    assert install.run_steps([retired, later], context, False, output, errors) == 2
    assert "failed retired state: could not remove " in errors.getvalue()
    assert "apply  later: pending" in output.getvalue()
