import contextlib
import io
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import agent_cli
import agent_store
import boot_history
import pytest
import tj_recover
from launch_contract import Liveness
from tj_agent.host import Host

ITEM = "github:github.com/tj-agents/agent:124"
LAUNCH = agent_cli.launch
ATTACH = agent_cli.attach_tmux


@dataclass
class FakeHost:
    root: Path
    live: dict[str, Liveness] = field(default_factory=dict)
    respawned: list[str] = field(default_factory=list)
    error: str | None = None
    SESSION_ENVIRONMENT: tuple[str, ...] = ()
    TAB_CLEARED: tuple[str, ...] = ()
    TAB_FORCED: dict[str, str] = field(default_factory=dict)

    def liveness(self, *, strict: bool = False) -> dict[str, Liveness]:
        if self.error:
            raise agent_cli.LaunchError(self.error)
        return self.live

    def transcript_modified(self, session_id: str) -> float:
        return (self.root / session_id).stat().st_mtime

    def respawn(self, background: str) -> None:
        self.respawned.append(background)

    def resume_arguments(
        self, directory: Path, title: str, session_id: str
    ) -> list[str]:
        return ["resume", session_id]

    def executable(self) -> str:
        return "fake-agent"


@dataclass
class Recovery:
    root: Path
    hosts: dict[Host, FakeHost]
    records: list[dict[str, Any]] = field(default_factory=list)
    launches: list[dict[str, Any]] = field(default_factory=list)
    launched: list[dict[str, Any]] = field(default_factory=list)
    ended: list[str] = field(default_factory=list)

    def session(
        self, host: Host, native: str, modified: float = 101.0, **fields: Any
    ) -> dict[str, Any]:
        transcript = self.hosts[host].root / native
        transcript.write_text("{}\n", encoding="utf-8")
        os.utime(transcript, (modified, modified))
        row = {
            "session": f"{host}:{native}",
            "native_id": native,
            "host": str(host),
            "state": "active",
            "cwd": str(self.root),
            "links": [],
            **fields,
        }
        self.records.append(row)
        return row

    def store(self, *arguments: str) -> dict[str, Any]:
        match arguments:
            case ("sessions",):
                return {"sessions": self.records}
            case ("sessions", "--item", _):
                return {"sessions": [row for row in self.records if row["links"]]}
            case ("show", handle):
                return {
                    "session": next(
                        row for row in self.records if row["session"] == handle
                    )
                }
            case ("launches",):
                return {"launches": self.launches}
            case ("set-open", "--launch-id", launch_id, "--open", value):
                launch = next(
                    row for row in self.launches if row["launch_id"] == launch_id
                )
                launch["open"] = value == "true"
                return {"launch": launch}
            case ("end", "--session", handle, "--reason", reason):
                self.ended.append(handle)
                row = next(row for row in self.records if row["session"] == handle)
                row.update(state="ended", end_reason=reason)
                return {"session": row}
            case _:
                raise AssertionError(arguments)

    def launch(
        self,
        host: Host,
        directory: Path,
        title: str,
        arguments: list[str],
        **options: Any,
    ) -> dict[str, Any]:
        self.launched.append(
            {
                "host": host,
                "directory": directory,
                "title": title,
                "arguments": arguments,
                **options,
            }
        )
        return {"launch": {"launch_id": "new-launch"}}

    def run(self, *arguments: str) -> tuple[int, list[dict[str, Any]]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = tj_recover.main(list(arguments))
        return code, json.loads(output.getvalue())


@pytest.fixture
def recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Recovery:
    hosts = {}
    for host in Host:
        directory = tmp_path / str(host)
        directory.mkdir()
        hosts[host] = FakeHost(directory)
    fixture = Recovery(tmp_path, hosts)
    monkeypatch.setattr(agent_store, "run", fixture.store)
    monkeypatch.setattr(agent_cli, "host_launch", lambda host: hosts[host])
    monkeypatch.setattr(agent_cli, "launch", fixture.launch)
    monkeypatch.setattr(tj_recover, "SESSIONS", hosts)
    monkeypatch.setattr(boot_history, "previous_boot", lambda: 100.0)
    monkeypatch.setattr(sys, "platform", "win32")
    return fixture


@pytest.mark.parametrize("host", list(Host))
def test_recover_previous_uptime_session_without_item(
    recovery: Recovery, host: Host
) -> None:
    recovery.session(host, "recent")
    code, rows = recovery.run()
    assert code == 0
    assert rows[0]["action"] == "resumed"
    assert recovery.launched[0]["arguments"] == ["resume", "recent"]
    assert recovery.launched[0]["item"] is None
    assert recovery.launched[0]["want_session"] is True


def test_recover_auto_ends_old_dead_and_leaves_live_alone(recovery: Recovery) -> None:
    recovery.session(Host.CODEX, "old", 99)
    recovery.session(Host.CLAUDE, "live", 99)
    recovery.hosts[Host.CLAUDE].live["live"] = Liveness(True, "busy")
    code, rows = recovery.run()
    assert code == 0
    assert recovery.ended == ["codex:old"]
    assert [(row["session"], row["action"]) for row in rows] == [("codex:old", "ended")]
    assert recovery.launched == []


def test_recover_older_includes_auto_ended_sessions_on_later_invocation(
    recovery: Recovery,
) -> None:
    recovery.session(Host.CODEX, "old", 99)
    recovery.run()
    recovery.run("--older")
    assert recovery.launched[0]["arguments"] == ["resume", "old"]


def test_recover_cleanly_ended_sessions_require_explicit_ended(
    recovery: Recovery,
) -> None:
    recovery.session(Host.CODEX, "finished", state="ended", end_reason="closed")
    assert recovery.run("--older")[1] == []
    assert recovery.run("--ended")[1][0]["action"] == "resumed"


def test_recover_dry_run_changes_nothing(recovery: Recovery) -> None:
    recovery.session(Host.CODEX, "recent")
    recovery.session(Host.CLAUDE, "old", 99)
    recovery.session(Host.CLAUDE, "background")
    recovery.hosts[Host.CLAUDE].live["background"] = Liveness(False, background="bg")
    before = json.dumps(recovery.records)
    code, rows = recovery.run("--dry-run")
    assert code == 0
    assert [row["action"] for row in rows] == ["resume", "end", "respawn"]
    assert not recovery.launched and not recovery.ended
    assert not recovery.hosts[Host.CLAUDE].respawned
    assert json.dumps(recovery.records) == before


def test_recover_background_respawns_in_place(recovery: Recovery) -> None:
    recovery.session(Host.CLAUDE, "background")
    recovery.hosts[Host.CLAUDE].live["background"] = Liveness(False, background="bg")
    assert recovery.run()[1][0]["action"] == "respawned"
    assert recovery.hosts[Host.CLAUDE].respawned == ["bg"]
    assert recovery.launched == []


def test_recover_preserves_launch_metadata_and_existing_links(
    recovery: Recovery,
) -> None:
    recovery.session(
        Host.CODEX,
        "recent",
        launch_id="original",
        title="session title",
        links=[{"item": {"ref": ITEM}, "role": "reviewer"}],
    )
    recovery.launches.append(
        {
            "launch_id": "original",
            "title": "recorded title",
            "item": {"ref": ITEM},
            "parent": "claude:parent",
            "worktree": str(recovery.root),
        }
    )
    code, rows = recovery.run()
    assert code == 0
    assert recovery.launched[0]["title"] == "recorded title"
    assert recovery.launched[0]["item"] == ITEM
    assert recovery.launched[0]["parent"] == "claude:parent"
    assert recovery.records[0]["links"][0]["role"] == "reviewer"
    assert rows[0]["previous_launch_id"] == "original"
    assert recovery.launches[0]["launch_id"] == "original"


def test_recover_missing_directory_is_reported_and_others_continue(
    recovery: Recovery,
) -> None:
    recovery.session(Host.CODEX, "gone", worktree=str(recovery.root / "missing"))
    recovery.session(Host.CLAUDE, "okay")
    code, rows = recovery.run()
    assert code == 1
    assert [row["action"] for row in rows] == ["failed", "resumed"]
    assert "missing" in rows[0]["error"]
    assert len(recovery.launched) == 1


def test_recover_missing_transcript_never_ends_session(recovery: Recovery) -> None:
    recovery.session(Host.CODEX, "missing")
    (recovery.hosts[Host.CODEX].root / "missing").unlink()
    code, rows = recovery.run("--dry-run")
    assert code == 1
    assert rows[0]["action"] == "failed"
    assert recovery.ended == []


@pytest.mark.parametrize("dry_run", [True, False])
def test_failed_liveness_never_ends_or_restores_affected_host(
    recovery: Recovery, dry_run: bool
) -> None:
    recovery.session(Host.CODEX, "old", 99)
    recovery.session(Host.CODEX, "recent")
    recovery.session(Host.CLAUDE, "okay")
    recovery.hosts[Host.CODEX].error = "probe failed"
    code, rows = recovery.run(*(["--dry-run"] if dry_run else []))
    assert code == 1
    assert [row["action"] for row in rows[:2]] == ["failed", "failed"]
    assert all(row["error"] == "probe failed" for row in rows[:2])
    assert recovery.ended == []
    assert len(recovery.launched) == (0 if dry_run else 1)


def test_recover_session_and_item_filters(recovery: Recovery) -> None:
    recovery.session(Host.CODEX, "unlinked")
    recovery.session(Host.CLAUDE, "linked", links=[{"item": {"ref": ITEM}}])
    assert recovery.run("124", "--dry-run")[1][0]["session"] == "claude:linked"
    assert (
        recovery.run("--session", "codex:unlinked", "--dry-run")[1][0]["session"]
        == "codex:unlinked"
    )


@pytest.mark.parametrize("windows", [True, False])
def test_recover_uses_windows_terminal_or_tmux_with_fake_processes(
    recovery: Recovery,
    monkeypatch: pytest.MonkeyPatch,
    windows: bool,
) -> None:
    recovery.session(Host.CODEX, "recent")
    commands = []

    def run(command: list[str], **options: Any) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="@2", stderr="")

    def launch(*arguments: Any, **options: Any) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            LAUNCH(*arguments, **options, run=run, environ={"TMUX": "fake"}),
        )

    monkeypatch.setattr(agent_cli, "IS_WINDOWS", windows)
    monkeypatch.setattr(agent_cli.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        agent_cli, "await_launch", lambda *a, **k: {"launch": {"launch_id": "new"}}
    )
    monkeypatch.setattr(agent_cli, "launch", launch)
    assert recovery.run()[0] == 0
    expected = "new-tab" if windows else "new-window"
    assert expected in commands[0]
    assert "run" in commands[0]
    assert "resume" in commands[0]
    assert "recent" in commands[0]


def test_recover_from_plain_linux_terminal_creates_then_attaches_tmux(
    recovery: Recovery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovery.session(Host.CLAUDE, "first")
    recovery.session(Host.CODEX, "second")
    commands = []
    exists = False

    def run(command: list[str], **options: Any) -> SimpleNamespace:
        nonlocal exists
        commands.append(command)
        if command[1] == "has-session":
            return SimpleNamespace(
                returncode=0 if exists else 1, stdout="", stderr="no session"
            )
        if command[1] == "new-session":
            exists = True
        return SimpleNamespace(returncode=0, stdout="@2", stderr="")

    def launch(*arguments: Any, **options: Any) -> dict[str, Any]:
        return cast(dict[str, Any], LAUNCH(*arguments, **options, run=run, environ={}))

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(agent_cli, "IS_WINDOWS", False)
    monkeypatch.setattr(agent_cli.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        agent_cli, "await_launch", lambda *a, **k: {"launch": {"launch_id": "new"}}
    )
    monkeypatch.setattr(agent_cli, "launch", launch)
    monkeypatch.setattr(
        agent_cli, "attach_tmux", lambda session: ATTACH(session, run=run)
    )
    assert recovery.run()[0] == 0
    assert [command[1] for command in commands].count("new-session") == 1
    assert [command[1] for command in commands].count("new-window") == 1
    assert commands[-1][1] == "attach-session"


def open_launch(
    recovery: Recovery, launch_id: str, session: str | None, opened: bool = True
) -> None:
    recovery.launches.append(
        {
            "launch_id": launch_id,
            "title": launch_id,
            "cwd": str(recovery.root),
            "session": session,
            "open": opened,
        }
    )


def test_recover_open_reopens_exactly_the_open_tabs_whose_agent_is_dead(
    recovery: Recovery,
) -> None:
    recovery.session(Host.CLAUDE, "was-open", 1)
    recovery.session(Host.CLAUDE, "closed")
    recovery.session(Host.CODEX, "running")
    recovery.hosts[Host.CODEX].live["running"] = Liveness(True, "busy")
    open_launch(recovery, "earlier", "claude:was-open")
    open_launch(recovery, "latest", "claude:was-open")
    open_launch(recovery, "shut", "claude:closed", opened=False)
    open_launch(recovery, "live", "codex:running")
    code, rows = recovery.run("--open")
    assert code == 0
    assert [(row["session"], row["action"]) for row in rows] == [
        ("claude:was-open", "resumed")
    ]
    assert recovery.launched[0]["title"] == "latest"
    assert recovery.launched[0]["arguments"] == ["resume", "was-open"]
    assert recovery.ended == []
    assert {launch["launch_id"]: launch["open"] for launch in recovery.launches} == {
        "earlier": False,
        "latest": False,
        "shut": False,
        "live": True,
    }


def test_recover_open_reports_a_launch_with_no_session(recovery: Recovery) -> None:
    open_launch(recovery, "never-registered", None)
    code, rows = recovery.run("--open")
    assert code == 1
    assert rows[0]["action"] == "failed"
    assert "never-registered" in rows[0]["error"]
    assert recovery.launched == []


def test_recover_closes_the_launch_it_reopens(recovery: Recovery) -> None:
    recovery.session(Host.CODEX, "recent", launch_id="original")
    open_launch(recovery, "original", "codex:recent")
    assert recovery.run()[1][0]["action"] == "resumed"
    assert recovery.launches[0]["open"] is False


def test_recover_open_dry_run_changes_nothing(recovery: Recovery) -> None:
    recovery.session(Host.CLAUDE, "was-open")
    open_launch(recovery, "tab", "claude:was-open")
    code, rows = recovery.run("--open", "--dry-run")
    assert code == 0
    assert rows[0]["action"] == "resume"
    assert recovery.launches[0]["open"] is True
    assert recovery.launched == []
