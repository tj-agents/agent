"""Native bootstrap precedes admission and acknowledges only installed releases."""

from __future__ import annotations

import importlib
import io
import os
import shutil
import sys
from pathlib import Path
from unittest import mock

import pytest
from tj_cli import native_install

RUNTIME = Path(__file__).resolve().parents[1] / "plugins/agent/.agents/agent/runtime"
sys.path.insert(0, str(RUNTIME))

import native_runtime  # noqa: E402

events = importlib.import_module("machine_events")
install = importlib.import_module("install")
launch = importlib.import_module("launch_check")
required = importlib.import_module("required_plugins")
reload = importlib.import_module("reload_plugins")
host = importlib.import_module("host")


def test_missing_pair_repairs_without_a_source_checkout_and_is_then_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "native bin"
    monkeypatch.setenv("TJ_BIN_DIR", str(directory))
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(tmp_path / "state"))
    monkeypatch.setenv("PATH", os.defpath)
    names = ("tj.exe", "tj_hook.exe") if os.name == "nt" else ("tj", "tj_hook")

    def install_pair(destination: Path, *, revision: str | None) -> tuple[str, ...]:
        assert destination == directory
        assert revision is None
        destination.mkdir(parents=True)
        for name in names:
            binary = destination / name
            binary.write_bytes(b"fixture")
            binary.chmod(0o755)
        return names

    with mock.patch.object(
        native_install, "ensure_native", side_effect=install_pair
    ) as ensure:
        assert native_runtime.prepare() == names
        assert native_runtime.prepare() == ()
    ensure.assert_called_once_with(directory, revision=None)
    assert os.environ["PATH"].split(os.pathsep)[0] == str(directory)


def test_launch_bootstraps_before_native_plugin_inventory(tmp_path: Path) -> None:
    order: list[str] = []

    def inventory(*arguments: object) -> dict[str, Path]:
        order.append("plugins")
        return {}

    with (
        mock.patch.object(required, "launch_context", return_value=(tmp_path, ())),
        mock.patch.object(
            native_runtime, "prepare", side_effect=lambda: order.append("native")
        ),
        mock.patch.object(
            reload,
            "prepare_required",
            side_effect=inventory,
        ),
    ):
        assert launch.prepare_launch_roots(tmp_path, host.Host.CODEX, "codex", []) == (
            tmp_path,
            {},
        )
    assert order == ["native", "plugins"]


def test_relative_override_stays_bound_when_the_host_changes_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TJ_BIN_DIR", "native")
    monkeypatch.setenv("PATH", os.defpath)
    directory = tmp_path.resolve() / "native"
    directory.mkdir()
    names = ("tj.exe", "tj_hook.exe") if os.name == "nt" else ("tj", "tj_hook")
    for name in names:
        binary = directory / name
        binary.write_bytes(b"fixture")
        binary.chmod(0o755)
    with mock.patch.object(native_install, "ensure_native") as ensure:
        assert native_runtime.prepare() == ()
    ensure.assert_not_called()
    host_directory = tmp_path / "host"
    host_directory.mkdir()
    monkeypatch.chdir(host_directory)
    assert native_install.default_bin_dir() == directory
    assert native_runtime.missing() == ()
    assert Path(shutil.which(names[1]) or "").resolve() == directory / names[1]


def test_maintenance_launch_does_not_install_native_tools(tmp_path: Path) -> None:
    with (
        mock.patch.object(required, "launch_context", return_value=(None, ())),
        mock.patch.object(native_runtime, "prepare") as prepare,
    ):
        assert launch.prepare_launch_roots(
            tmp_path, host.Host.CODEX, "codex", ["--help"]
        ) == (None, None)
    prepare.assert_not_called()


def test_cli_revision_is_not_acknowledged_until_its_release_is_installed() -> None:
    revision = "a" * 40
    current = {
        "nameWithOwner": "tj-agents/cli",
        "defaultBranchRef": {"name": "main", "target": {"oid": revision}},
    }
    reconciler = events.Reconciler(
        refresh=mock.Mock(return_value=0), policy=mock.Mock()
    )
    with (
        mock.patch.object(
            native_runtime, "prepare", side_effect=RuntimeError("release not published")
        ),
        pytest.raises(RuntimeError, match="not published"),
    ):
        reconciler.repository(current)
    assert "tj-agents/cli" not in reconciler.state
    with mock.patch.object(native_runtime, "prepare", return_value=()) as prepare:
        reconciler.repository(current)
        reconciler.repository(current)
    prepare.assert_called_once_with(revision=revision)
    assert reconciler.state["tj-agents/cli"]["revision"] == revision


def test_installer_check_reports_missing_pair_without_installing(
    tmp_path: Path,
) -> None:
    context = install.Context(home=tmp_path, environ={}, windows=os.name == "nt")
    with (
        mock.patch.object(native_runtime, "missing", return_value=("tj_hook",)),
        mock.patch.object(native_runtime, "prepare") as prepare,
    ):
        assert "tj_hook" in install.check_native_cli(context)[0]
    prepare.assert_not_called()


def test_failed_native_install_prevents_dependent_steps(tmp_path: Path) -> None:
    context = install.Context(home=tmp_path, environ={}, windows=os.name == "nt")
    native = next(step for step in install.STEPS if step.name == "native CLI")
    dependent = mock.Mock(return_value=[])
    plugin = install.Step("plugin setup", dependent, dependent)
    with (
        mock.patch.object(native_runtime, "missing", return_value=("tj_hook",)),
        mock.patch.object(
            native_runtime, "prepare", side_effect=RuntimeError("release unavailable")
        ),
    ):
        assert (
            install.run_steps(
                [native, plugin], context, False, io.StringIO(), io.StringIO()
            )
            == 2
        )
    dependent.assert_not_called()


def test_explicit_refresh_bootstraps_before_any_native_admission(
    tmp_path: Path,
) -> None:
    with (
        mock.patch.object(
            native_runtime, "prepare", side_effect=RuntimeError("release unavailable")
        ),
        mock.patch.object(reload, "prepare_required") as inventory,
    ):
        assert reload.main(["--all", "--project", str(tmp_path)]) == 1
    inventory.assert_not_called()


def test_installer_check_converges_when_parent_shell_path_omits_native_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TJ_BIN_DIR", str(tmp_path / "native"))
    monkeypatch.setenv("PATH", os.defpath)

    def context() -> object:
        return install.Context(
            home=tmp_path, environ={"PATH": os.defpath}, windows=os.name == "nt"
        )

    with (
        mock.patch.object(install, "machine_context", side_effect=context),
        mock.patch.object(events, "require_tommy"),
        mock.patch.object(native_runtime, "missing", return_value=()),
        mock.patch.object(native_runtime, "prepare") as prepare,
    ):
        assert install.main(["--check", "--step", "native CLI"]) == 0
        assert install.main(["--check", "--step", "native CLI"]) == 0
    prepare.assert_not_called()


def test_supervisor_retains_the_selected_native_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "native bin ü"
    context = install.Context(
        home=tmp_path, environ={"TJ_BIN_DIR": str(directory)}, windows=os.name == "nt"
    )
    service = importlib.import_module("event_service")
    assert "--native-bin-dir=" + str(directory.resolve()) in service.arguments(context)
    monkeypatch.delenv("TJ_BIN_DIR", raising=False)
    with (
        mock.patch.object(events, "require_tommy"),
        mock.patch.object(events, "catch_up_states", return_value=[]),
        mock.patch.object(events, "Reconciler") as reconciler,
    ):
        reconciler.return_value.state = {}
        reconciler.return_value.catch_up.return_value = []
        assert (
            events.main(
                [
                    "--native-bin-dir",
                    str(directory),
                    "--state-directory",
                    str(tmp_path / "state"),
                ]
            )
            == 0
        )
    assert native_install.default_bin_dir() == directory.resolve()
