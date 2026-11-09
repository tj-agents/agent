import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent/.agents/agent/runtime"))

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
    removed = retired_state.remove(tmp_path, windows=True, run=Recorder())
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
    removed = retired_state.remove(tmp_path, windows=True, run=run)
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
    removed = retired_state.remove(tmp_path, windows=False, home=home, run=run)
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
    )
    assert removed == []


def test_a_missing_process_launcher_never_breaks_the_caller(tmp_path: Path) -> None:
    seed(tmp_path)

    def missing(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("tj_hook is not installed on PATH")

    removed = retired_state.remove(tmp_path, windows=True, run=missing)
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
