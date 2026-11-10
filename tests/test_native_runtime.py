"""Native bootstrap acknowledges only installed releases."""

from __future__ import annotations

import importlib
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from tj_cli import native_install

RUNTIME = Path(__file__).resolve().parents[1] / "plugins/agent/.agents/agent/runtime"
sys.path.insert(0, str(RUNTIME))

import native_runtime  # noqa: E402

install = importlib.import_module("install")


@pytest.fixture(autouse=True)
def isolate_process_git_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name == "GIT_CONFIG_COUNT" or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            monkeypatch.delenv(name, raising=False)


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

    with (
        mock.patch.object(
            native_install, "ensure_native", side_effect=install_pair
        ) as ensure,
        mock.patch.object(
            native_install,
            "_run",
            return_value=subprocess.CompletedProcess([], 0, "tj_hook 0.2.0\n", ""),
        ),
    ):
        assert native_runtime.prepare() == names
        assert native_runtime.prepare() == ()
    ensure.assert_called_once_with(directory, revision=None)
    assert os.environ["PATH"].split(os.pathsep)[0] == str(directory)


def test_existing_outdated_pair_is_upgraded_before_exposure_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "native bin"
    directory.mkdir()
    names = ("tj.exe", "tj_hook.exe") if os.name == "nt" else ("tj", "tj_hook")
    for name in names:
        (directory / name).write_bytes(b"old pair")
    monkeypatch.setenv("TJ_BIN_DIR", str(directory))
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(tmp_path / "state"))
    ready = False

    def upgrade(destination: Path, *, revision: str | None) -> tuple[str, ...]:
        nonlocal ready
        assert destination == directory
        assert all((directory / name).read_bytes() == b"old pair" for name in names)
        ready = True
        return names

    with (
        mock.patch.object(
            native_install,
            "pending_native",
            side_effect=lambda _: () if ready else names,
        ),
        mock.patch.object(
            native_install, "ensure_native", side_effect=upgrade
        ) as ensure,
        mock.patch.object(native_runtime, "expose") as expose,
    ):
        assert native_runtime.prepare() == names
        assert native_runtime.prepare() == ()
    ensure.assert_called_once_with(directory, revision=None)
    assert expose.call_count == 2


def test_old_release_noop_cannot_admit_incompatible_native_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "native bin"
    monkeypatch.setenv("TJ_BIN_DIR", str(directory))
    monkeypatch.setenv("AGENT_STATE_DIRECTORY", str(tmp_path / "state"))
    with (
        mock.patch.object(native_install, "pending_native", return_value=("tj_hook",)),
        mock.patch.object(native_install, "ensure_native", return_value=()) as ensure,
        mock.patch.object(native_runtime, "expose") as expose,
        pytest.raises(
            RuntimeError, match=r"not compatible.*compatible published CLI release"
        ),
    ):
        native_runtime.prepare()
    ensure.assert_called_once_with(directory, revision=None)
    expose.assert_not_called()


def test_native_runtime_installs_then_exposes_without_touching_git_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "native bin"
    monkeypatch.setenv("TJ_BIN_DIR", str(directory))
    monkeypatch.setenv("PATH", os.pathsep.join(("original-one", "original-two")))
    events: list[str] = []
    ready = False

    def pending() -> tuple[str, ...]:
        events.append("inventory")
        return () if ready else ("tj_hook",)

    def ensure(destination: Path, *, revision: str | None) -> tuple[str, ...]:
        nonlocal ready
        events.append("installer")
        ready = True
        return ("tj_hook",)

    with (
        mock.patch.object(native_runtime, "pending", side_effect=pending),
        mock.patch.object(native_install, "ensure_native", side_effect=ensure),
    ):
        native_runtime.prepare()

    assert events[:2] == ["inventory", "installer"]
    assert not [
        name
        for name in os.environ
        if name == "GIT_CONFIG_COUNT"
        or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    ]
    assert os.environ["TJ_BIN_DIR"] == str(directory)
    assert os.environ["PATH"].split(os.pathsep) == [
        str(directory),
        "original-one",
        "original-two",
    ]


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
    with (
        mock.patch.object(native_install, "ensure_native") as ensure,
        mock.patch.object(
            native_install, "pending_native", return_value=()
        ) as readiness,
    ):
        assert native_runtime.prepare() == ()
        assert native_runtime.pending() == ()
    ensure.assert_not_called()
    readiness.assert_called_with(directory)
    host_directory = tmp_path / "host"
    host_directory.mkdir()
    monkeypatch.chdir(host_directory)
    assert native_install.default_bin_dir() == directory
    assert Path(shutil.which(names[1]) or "").resolve() == directory / names[1]


def test_installer_check_reports_missing_pair_without_installing(
    tmp_path: Path,
) -> None:
    context = install.Context(home=tmp_path, environ={}, windows=os.name == "nt")
    with (
        mock.patch.object(native_runtime, "pending", return_value=("tj_hook",)),
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
        mock.patch.object(native_runtime, "pending", return_value=("tj_hook",)),
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
        mock.patch.object(native_runtime, "pending", return_value=()),
        mock.patch.object(native_runtime, "prepare") as prepare,
    ):
        assert install.main(["--check", "--step", "native CLI"]) == 0
        assert install.main(["--check", "--step", "native CLI"]) == 0
    prepare.assert_not_called()


def test_install_requires_the_release_that_implements_safe_resume(
    tmp_path: Path,
) -> None:
    context = install.Context(home=tmp_path, environ={}, windows=os.name == "nt")
    with (
        mock.patch.object(
            native_runtime, "pending", return_value=("older revision",)
        ) as pending,
        mock.patch.object(native_runtime, "prepare", return_value=()) as prepare,
    ):
        assert install.check_native_cli(context)
        install.apply_native_cli(context)
    pending.assert_called_once_with(revision=native_runtime.NATIVE_REVISION)
    prepare.assert_called_once_with(revision=native_runtime.NATIVE_REVISION)
