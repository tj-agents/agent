import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import install
import jobs
import plugin_update
import pytest
import tj_plugins
from tj_agent.host import Host
from tj_cli import process
from tree_digest import Digests

PACKAGE = Path(__file__).resolve().parents[1] / "plugins/agent"
RUNTIME = ".agents/agent/runtime/tj_plugins.py"


def test_concurrent_update_does_not_mutate_or_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*_args: object) -> bool:
        pytest.fail("A concurrent update must not begin native inventory or mutation")

    monkeypatch.setattr(plugin_update, "marketplace_revision", unexpected)
    with jobs.held(tmp_path, jobs.Job.PLUGINS) as acquired:
        assert acquired
        outcome = plugin_update.update(Host.CLAUDE, Digests(tmp_path), {})
        assert jobs.running(tmp_path, jobs.Job.PLUGINS)
    assert outcome.failures == [
        "Another plugin update is running; retry after it completes"
    ]
    assert not outcome.changed and outcome.revision is None
    assert not jobs.running(tmp_path, jobs.Job.PLUGINS)


def cached_update(
    *_args: object, converge_runtime: Callable[[], str | None]
) -> plugin_update.Outcome:
    problem = converge_runtime()
    return plugin_update.Outcome([problem] if problem else [], False, "revision")


def test_a_plugin_update_moves_the_tj_commands_to_the_installed_agent_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(state))
    previous = tmp_path / "previous"
    shutil.copytree(PACKAGE / ".agents", previous / ".agents")
    (previous / RUNTIME).write_text("previous runtime", encoding="utf-8")
    install.install_runtime(state / "bin", previous)
    monkeypatch.setattr(plugin_update, "update", cached_update)
    monkeypatch.setattr(plugin_update, "agent_package", lambda *_: PACKAGE)
    monkeypatch.setattr(tj_plugins, "publish", lambda *_: None)
    calls: list[list[str]] = []

    def installer(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert Path(command[2]) == PACKAGE / ".agents/agent/runtime/install.py"
        if "--check" not in command:
            install.install_runtime(state / "bin", PACKAGE)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(process, "run", installer)

    assert tj_plugins.main(["update", "--host", "codex"]) == 0

    installed = state / "bin" / RUNTIME
    assert installed.read_bytes() == (PACKAGE / RUNTIME).read_bytes()
    assert len(calls) == 2
    assert "native CLI" in calls[0]
    assert "tj lifecycle commands" in calls[0]
    assert "shell profiles" in calls[0]
    assert "--check" in calls[1]


@pytest.mark.parametrize("failed_check", [False, True])
def test_runtime_failure_prevents_publication_and_retries_without_plugin_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_check: bool
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(tmp_path / "state"))
    monkeypatch.setattr(plugin_update, "agent_package", lambda *_: PACKAGE)
    monkeypatch.setattr(plugin_update, "update", cached_update)
    published: list[str] = []
    failing = True

    def installer(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        failed = failing and (("--check" in command) == failed_check)
        return subprocess.CompletedProcess(
            command, int(failed), "", "release unavailable" if failed else ""
        )

    def publish(_host: Host, revision: str) -> None:
        published.append(revision)

    monkeypatch.setattr(process, "run", installer)
    monkeypatch.setattr(tj_plugins, "publish", publish)

    assert tj_plugins.main(["update", "--host", "claude"]) == 1
    assert published == []
    failing = False
    assert tj_plugins.main(["update", "--host", "claude"]) == 0
    assert published == ["revision"]


def test_missing_updated_package_cannot_report_convergence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plugin_update, "agent_package", lambda *_: None)
    assert "unverified" in str(tj_plugins.refresh_runtime(Host.CLAUDE))


@pytest.mark.parametrize("failure", [None, "release unavailable"])
def test_runtime_converges_before_session_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(plugin_update, "already_current", lambda *_: True)
    monkeypatch.setattr(plugin_update, "behind_remote", lambda *_: False)
    monkeypatch.setattr(plugin_update, "prefetch_standards", lambda *_: None)
    monkeypatch.setattr(plugin_update, "update_standards", lambda *_: None)
    monkeypatch.setattr(plugin_update, "marketplace_revision", lambda *_: "revision")
    installed = plugin_update.INSTALLED[Host.CLAUDE]
    monkeypatch.setattr(installed, "update_marketplaces", lambda *_: None)
    monkeypatch.setattr(installed, "activate", lambda *_: events.append("activate"))

    def converge() -> str | None:
        events.append("runtime")
        return failure

    result = plugin_update.update(
        Host.CLAUDE, Digests(tmp_path), {}, converge_runtime=converge
    )
    assert events == (["runtime"] if failure else ["runtime", "activate"])
    assert bool(result.failures) == bool(failure)
