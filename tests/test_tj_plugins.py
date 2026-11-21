import shutil
import subprocess
from pathlib import Path

import install
import jobs
import plugin_update
import pytest
import tj_plugins
from tj_agent.host import Host
from tj_cli import process

PACKAGE = Path(__file__).resolve().parents[1] / "plugins/agent"
RUNTIME = ".agents/agent/runtime/tj_plugins.py"


def test_a_plugin_update_moves_the_tj_commands_to_the_installed_agent_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(state))
    previous = tmp_path / "previous"
    shutil.copytree(PACKAGE / ".agents", previous / ".agents")
    (previous / RUNTIME).write_text("previous runtime", encoding="utf-8")
    install.install_runtime(state / "bin", previous)
    monkeypatch.setattr(
        plugin_update, "update", lambda *_: plugin_update.Outcome([], False, None)
    )
    monkeypatch.setattr(plugin_update, "agent_package", lambda *_: PACKAGE)

    assert tj_plugins.main(["update", "--host", "codex"]) == 0

    installed = state / "bin" / RUNTIME
    assert installed.read_bytes() == (PACKAGE / RUNTIME).read_bytes()


REVISION = "a" * 40


def run_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    changed: bool,
    record: str | None,
    failures: list[str] | None = None,
    publish_code: int = 0,
) -> tuple[int, list[list[str]]]:
    """Run `tj-plugins update --host claude` against tj's record in `tmp_path`."""
    cache = tmp_path / "tj-cache"
    monkeypatch.setenv("TJ_CACHE_DIR", str(cache))
    if record is not None:
        (cache / "published").mkdir(parents=True)
        (cache / "published" / "claude").write_text(
            f"{record}\n1795214751827071300.44136", encoding="utf-8"
        )
    commands: list[list[str]] = []

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, publish_code, "", "tj is gone")

    monkeypatch.setattr(jobs, "state_directory", lambda: tmp_path / "state")
    monkeypatch.setattr(
        plugin_update,
        "update",
        lambda *_: plugin_update.Outcome(failures or [], changed, REVISION),
    )
    monkeypatch.setattr(tj_plugins, "refresh_runtime", lambda _host: None)
    monkeypatch.setattr(process, "run", run)
    return tj_plugins.main(["update", "--host", "claude"]), commands


PUBLISH = [["tj", "update", "--host", "claude", "--revision", REVISION]]


@pytest.mark.parametrize("record", [None, "b" * 40, ""])
def test_a_stale_published_record_is_published_though_no_plugin_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: str | None
) -> None:
    code, commands = run_update(tmp_path, monkeypatch, changed=False, record=record)

    assert code == 0
    assert commands == PUBLISH


def test_a_record_at_the_revision_is_not_published_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, commands = run_update(tmp_path, monkeypatch, changed=False, record=REVISION)

    assert code == 0
    assert commands == []


def test_a_plugin_that_changed_is_published_even_when_the_record_is_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, commands = run_update(tmp_path, monkeypatch, changed=True, record=REVISION)

    assert code == 0
    assert commands == PUBLISH


def test_a_failed_update_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, commands = run_update(
        tmp_path, monkeypatch, changed=True, record=None, failures=["claude: boom"]
    )

    assert code == 1
    assert commands == []


def test_a_publish_that_tj_rejects_fails_the_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _commands = run_update(
        tmp_path, monkeypatch, changed=False, record=None, publish_code=1
    )

    assert code == 1
    assert "tj update failed: tj is gone" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("environ", "windows", "expected"),
    [
        ({"TJ_CACHE_DIR": "/cache"}, False, "/cache/published/claude"),
        ({"TJ_CACHE_DIR": "/cache"}, True, "/cache/published/claude"),
        ({"XDG_CACHE_HOME": "/xdg"}, False, "/xdg/tj/published/claude"),
        ({}, False, "/home/me/.cache/tj/published/claude"),
        ({"LOCALAPPDATA": "/local"}, True, "/local/tj/published/claude"),
        ({}, True, "/home/me/AppData/Local/tj/published/claude"),
    ],
)
def test_the_published_record_is_where_tj_reads_it(
    environ: dict[str, str], windows: bool, expected: str
) -> None:
    record = plugin_update.published_record(
        Host.CLAUDE, environ, Path("/home/me"), windows
    )

    assert record == Path(expected)
