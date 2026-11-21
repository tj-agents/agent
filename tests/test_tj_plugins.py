import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import install
import jobs
import native_runtime
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
    host: Host,
    digests: Digests,
    _environ: object,
    *,
    converge_runtime: Callable[[], str | None],
) -> plugin_update.Outcome:
    problem = converge_runtime()
    revision = "revision"
    if problem:
        return plugin_update.Outcome([problem], False, revision)
    path = digests.path.parent / f"activated-{host}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(revision, encoding="utf-8")
    return plugin_update.Outcome(
        [], False, revision, plugin_update.ActivationReceipt(path, revision)
    )


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
    monkeypatch.setattr(plugin_update.Outcome, "publish", lambda *_: None)
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
    published: list[list[str]] = []
    failing = True

    def installer(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[:2] == [str(native_runtime.executable()), "update"]:
            published.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        failed = failing and (("--check" in command) == failed_check)
        return subprocess.CompletedProcess(
            command, int(failed), "", "release unavailable" if failed else ""
        )

    monkeypatch.setattr(process, "run", installer)

    assert tj_plugins.main(["update", "--host", "claude"]) == 1
    assert published == []
    failing = False
    assert tj_plugins.main(["update", "--host", "claude"]) == 0
    assert published == [[*publish_command("revision"), "--if-stale"]]


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


REVISION = "a" * 40


def publish_command(revision: str = REVISION) -> list[str]:
    return [
        str(native_runtime.executable()),
        "update",
        "--host",
        "claude",
        "--revision",
        revision,
    ]


def run_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    changed: bool,
    failures: list[str] | None = None,
    publish_code: int = 0,
) -> tuple[int, list[list[str]]]:
    """Run the CLI entry point with a supplied update outcome."""
    commands: list[list[str]] = []

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, publish_code, "", "tj is gone")

    state = tmp_path / "state"
    state.mkdir()
    receipt = state / "activated-claude.txt"
    receipt.write_text(REVISION, encoding="utf-8")
    monkeypatch.setattr(jobs, "state_directory", lambda: state)
    monkeypatch.setenv("TJ_BIN_DIR", str(tmp_path / "verified-bin"))
    monkeypatch.setenv("PATH", str(tmp_path / "older-bin"), prepend=os.pathsep)
    monkeypatch.setattr(
        plugin_update,
        "update",
        lambda *_args, **_kwargs: plugin_update.Outcome(
            failures or [],
            changed,
            REVISION,
            plugin_update.ActivationReceipt(receipt, REVISION),
        ),
    )
    monkeypatch.setattr(process, "run", run)
    return tj_plugins.main(["update", "--host", "claude"]), commands


def test_an_unchanged_update_asks_tj_to_publish_only_a_stale_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, commands = run_update(tmp_path, monkeypatch, changed=False)

    assert code == 0
    assert commands == [[*publish_command(), "--if-stale"]]
    assert Path(commands[0][0]).parent == (tmp_path / "verified-bin").resolve()


def test_a_plugin_that_changed_is_always_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, commands = run_update(tmp_path, monkeypatch, changed=True)

    assert code == 0
    assert commands == [publish_command()]


def test_a_failed_update_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, commands = run_update(
        tmp_path, monkeypatch, changed=True, failures=["claude: boom"]
    )

    assert code == 1
    assert commands == []


def test_a_publish_that_tj_rejects_fails_the_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _commands = run_update(tmp_path, monkeypatch, changed=False, publish_code=1)

    assert code == 1
    assert "tj update failed: tj is gone" in capsys.readouterr().err


def test_failed_publication_retries_repairs_at_the_same_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "activated-claude.txt"
    receipt.write_text(REVISION, encoding="utf-8")
    current = False
    calls: list[list[str]] = []

    def repair(*_args: object) -> None:
        nonlocal current
        current = True

    def native(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command, int(len(calls) == 1), "", "publish failed"
        )

    installed = plugin_update.INSTALLED[Host.CLAUDE]
    monkeypatch.setattr(installed, "update_marketplaces", lambda *_args: None)
    monkeypatch.setattr(installed, "refresh", repair)
    monkeypatch.setattr(plugin_update, "already_current", lambda *_args: current)
    monkeypatch.setattr(plugin_update, "marketplace_revision", lambda *_args: REVISION)
    monkeypatch.setattr(plugin_update, "behind_remote", lambda *_args: False)
    monkeypatch.setattr(plugin_update, "prefetch_standards", lambda *_args: None)
    monkeypatch.setattr(plugin_update, "update_standards", lambda *_args: None)

    first = plugin_update.update(Host.CLAUDE, Digests(tmp_path), {})
    assert first.changed
    assert first.publish(Host.CLAUDE, native) == "publish failed"
    assert receipt.read_text(encoding="utf-8") != REVISION

    retry = plugin_update.update(Host.CLAUDE, Digests(tmp_path), {})
    assert not retry.changed
    assert retry.publish(Host.CLAUDE, native) is None
    assert receipt.read_text(encoding="utf-8") == REVISION

    settled = plugin_update.update(Host.CLAUDE, Digests(tmp_path), {})
    assert settled.publish(Host.CLAUDE, native) is None
    assert calls == [
        publish_command(),
        publish_command(),
        [*publish_command(), "--if-stale"],
    ]


@pytest.mark.parametrize("older_receipt", [REVISION, "older activation"])
def test_superseded_activation_cannot_publish_or_overwrite_the_receipt(
    tmp_path: Path, older_receipt: str
) -> None:
    receipt = tmp_path / "activated-claude.txt"
    receipt.write_text("newer activation", encoding="utf-8")

    def unexpected(
        *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        pytest.fail("A superseded activation cannot call the native publisher")

    outcome = plugin_update.Outcome(
        [], False, REVISION, plugin_update.ActivationReceipt(receipt, older_receipt)
    )
    assert (
        outcome.publish(Host.CLAUDE, unexpected)
        == "plugin publication was superseded; retry the update"
    )
    assert receipt.read_text(encoding="utf-8") == "newer activation"
