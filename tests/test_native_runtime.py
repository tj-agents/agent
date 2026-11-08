"""Native bootstrap precedes admission and acknowledges only installed releases."""

from __future__ import annotations

import importlib
import io
import json
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

events = importlib.import_module("machine_events")
autoupdate = importlib.import_module("plugin_autoupdate")
install = importlib.import_module("install")
launch = importlib.import_module("launch_check")
required = importlib.import_module("required_plugins")
reload = importlib.import_module("reload_plugins")
host = importlib.import_module("host")
startup_binding = importlib.import_module("startup_binding")


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


def test_launch_bootstraps_before_native_plugin_inventory(tmp_path: Path) -> None:
    order: list[str] = []

    def inventory(*arguments: object, admission: bool) -> dict[str, Path]:
        assert admission
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


@pytest.mark.parametrize("host_name", ["claude", "codex"])
@pytest.mark.parametrize("startup", [False, True])
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("failure", ["busy", "runtime", "os"])
def test_native_preparation_failure_blocks_only_managed_launches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host_name: str,
    startup: bool,
    managed: bool,
    failure: str,
) -> None:
    project = tmp_path / "selected project ü"
    (project / ".agents").mkdir(parents=True)
    (project / ".agents/plugins.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plugins": ["agent@tj-agents" if managed else "other@example"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TJ_BIN_DIR", str(tmp_path / "native"))
    error = (
        RuntimeError("native release unavailable")
        if failure == "runtime"
        else OSError("native install unavailable")
    )
    with (
        mock.patch.object(required, "launch_context", return_value=(project, ())),
        mock.patch.object(
            startup_binding.SESSIONS[host.Host.CODEX],
            "requires_verified_handoff",
            return_value=False,
        ),
        mock.patch.object(native_runtime, "pending", return_value=("tj_hook",)),
        mock.patch.object(
            autoupdate,
            "state_directory",
            return_value=tmp_path / "state",
        ),
        mock.patch.object(
            autoupdate,
            "acquire_lock",
            return_value=None if failure == "busy" else mock.Mock(),
        ),
        mock.patch.object(
            native_install, "ensure_native", side_effect=error
        ) as install,
        mock.patch.object(reload, "prepare_required") as inventory,
        mock.patch.object(launch, "recover_install") as recover,
        mock.patch.object(launch, "check", return_value="current") as check,
        mock.patch.object(startup_binding.managed_admission, "verify") as verify,
        mock.patch.object(startup_binding, "run_session", return_value=0) as native,
        mock.patch.object(startup_binding, "_prelaunch_terminal", return_value=None),
        mock.patch.object(
            startup_binding.register_session, "terminal_identity", return_value=None
        ),
    ):
        arguments = [
            "--host",
            host_name,
            "--project",
            str(tmp_path),
            "--target",
            "native",
        ]
        if startup:
            arguments.append("--require-session-start")
        entrypoint = startup_binding.main if startup else launch.main
        result = entrypoint(arguments)

    assert result == int(managed)
    inventory.assert_not_called()
    recover.assert_not_called()
    verify.assert_not_called()
    if failure == "busy":
        install.assert_not_called()
    else:
        install.assert_called_once_with(tmp_path / "native", revision=None)
    if managed:
        native.assert_not_called()
        check.assert_not_called()
        assert "Managed readiness" in capsys.readouterr().err
    elif startup:
        native.assert_called_once()
        assert "require_session_start" not in native.call_args.kwargs
    else:
        check.assert_not_called()
        assert "launching anyway" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "nt", reason="Windows Git configuration validation")
def test_native_runtime_reports_invalid_git_configuration_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "invalid")
    with (
        pytest.raises(
            RuntimeError,
            match="invalid process Git configuration: invalid GIT_CONFIG_COUNT",
        ),
    ):
        native_runtime.prepare_git_environment()


def test_native_runtime_prepares_git_before_install_and_preserves_path_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name == "GIT_CONFIG_COUNT" or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            monkeypatch.delenv(name, raising=False)
    directory = tmp_path / "native bin"
    monkeypatch.setenv("TJ_BIN_DIR", str(directory))
    monkeypatch.setenv("PATH", os.pathsep.join(("original-one", "original-two")))
    events: list[str] = []

    def prepare_git() -> None:
        events.append("git")
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = "core.longpaths"
        os.environ["GIT_CONFIG_VALUE_0"] = "true"

    ready = False

    def pending() -> tuple[str, ...]:
        events.append("inventory")
        return () if ready else ("tj_hook",)

    def ensure(destination: Path, *, revision: str | None) -> tuple[str, ...]:
        nonlocal ready
        assert os.environ["GIT_CONFIG_COUNT"] == "1"
        assert os.environ["GIT_CONFIG_KEY_0"].casefold() == "core.longpaths"
        assert os.environ["GIT_CONFIG_VALUE_0"].casefold() == "true"
        events.append("installer")
        ready = True
        return ("tj_hook",)

    with (
        mock.patch.object(
            native_runtime, "prepare_git_environment", side_effect=prepare_git
        ),
        mock.patch.object(native_runtime, "pending", side_effect=pending),
        mock.patch.object(native_install, "ensure_native", side_effect=ensure),
    ):
        native_runtime.prepare()
        prepared = dict(os.environ)

    assert events[:3] == ["git", "inventory", "installer"]
    assert events.count("git") == 2
    assert os.environ["GIT_CONFIG_COUNT"] == prepared["GIT_CONFIG_COUNT"] == "1"
    assert os.environ["TJ_BIN_DIR"] == str(directory)
    assert os.environ["PATH"].split(os.pathsep) == [
        str(directory),
        "original-one",
        "original-two",
    ]


def test_native_runtime_git_environment_preparation_is_idempotent() -> None:
    native_runtime.prepare_git_environment()
    prepared = {
        name: value
        for name, value in os.environ.items()
        if name == "GIT_CONFIG_COUNT"
        or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }

    native_runtime.prepare_git_environment()

    repeated = {
        name: value
        for name, value in os.environ.items()
        if name == "GIT_CONFIG_COUNT"
        or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }
    assert repeated == prepared
    if os.name == "nt":
        assert prepared["GIT_CONFIG_COUNT"] == "1"
        assert prepared["GIT_CONFIG_KEY_0"].casefold() == "core.longpaths"
        assert prepared["GIT_CONFIG_VALUE_0"].casefold() == "true"
    else:
        assert prepared == {}


def test_native_git_preparation_preserves_empty_inherited_configuration(
    tmp_path: Path,
) -> None:
    script = tmp_path / "prepare native git.py"
    script.write_text(
        "import subprocess\n"
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import native_runtime\n"
        "native_runtime.prepare_git_environment()\n"
        "native_runtime.prepare_git_environment()\n"
        "result = subprocess.run(\n"
        "    ['git', 'config', '--get', 'credential.helper'],\n"
        "    timeout=10, check=False,\n"
        ")\n"
        "raise SystemExit(result.returncode)\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.update(
        GIT_CONFIG_COUNT="2",
        GIT_CONFIG_KEY_0="credential.helper",
        GIT_CONFIG_VALUE_0="",
        GIT_CONFIG_KEY_1="core.askPass",
        GIT_CONFIG_VALUE_1="",
    )

    result = subprocess.run(
        [sys.executable, "-B", str(script), str(RUNTIME)],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


@pytest.mark.parametrize("host_name", ["claude", "codex"])
def test_startup_prepares_git_before_skipped_plugin_admission(
    tmp_path: Path, host_name: str
) -> None:
    events: list[str] = []

    def prepare_roots(*_arguments: object, **_keywords: object) -> tuple[None, None]:
        events.append("admission")
        return None, None

    def run_session(*_arguments: object, **_keywords: object) -> int:
        events.append("native frontend")
        return 0

    with (
        mock.patch.object(
            startup_binding.native_runtime,
            "prepare_git_environment",
            side_effect=lambda: events.append("git"),
        ),
        mock.patch.object(
            startup_binding.launch_check,
            "prepare_launch_roots",
            side_effect=prepare_roots,
        ),
        mock.patch.object(
            startup_binding.launch_check, "check", return_value="skipped"
        ),
        mock.patch.object(startup_binding, "run_session", side_effect=run_session),
        mock.patch.object(startup_binding, "_prelaunch_terminal", return_value=None),
        mock.patch.object(
            startup_binding.register_session, "terminal_identity", return_value=None
        ),
    ):
        result = startup_binding.main(
            [
                "--host",
                host_name,
                "--project",
                str(tmp_path),
                "--target",
                "native",
                "--",
                "--help",
            ]
        )

    assert result == 0
    assert events == ["git", "admission", "native frontend"]


def test_installer_snapshots_prepared_git_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, str]] = []
    events: list[str] = []

    def prepare_git() -> None:
        events.append("git")
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = "core.longpaths"
        os.environ["GIT_CONFIG_VALUE_0"] = "true"

    def context() -> object:
        events.append("snapshot")
        environment = dict(os.environ)
        captured.append(environment)
        return mock.Mock(environ=environment)

    for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.delenv(name, raising=False)
    with (
        mock.patch.object(
            install.native_runtime, "prepare_git_environment", side_effect=prepare_git
        ),
        mock.patch.object(install, "machine_context", side_effect=context),
        mock.patch.object(install.native_runtime, "expose"),
        mock.patch.object(
            install.native_runtime, "search_path", side_effect=lambda path: path
        ),
        mock.patch.object(install, "run_steps", return_value=0),
    ):
        assert install.main(["--check", "--step", "native CLI"]) == 0

    assert events == ["git", "snapshot"]
    assert captured[0]["GIT_CONFIG_COUNT"] == "1"
    assert captured[0]["GIT_CONFIG_KEY_0"] == "core.longpaths"
    assert captured[0]["GIT_CONFIG_VALUE_0"] == "true"


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
    reconciler = events.Reconciler(refresh=mock.Mock(return_value=0))
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


@pytest.mark.parametrize("check_only", [False, True])
def test_fresh_install_bootstraps_before_identity_checks(
    tmp_path: Path, check_only: bool
) -> None:
    context = install.Context(home=tmp_path, environ={}, windows=os.name == "nt")
    ready = False

    def prepare() -> tuple[str, ...]:
        nonlocal ready
        ready = True
        return ("tj", "tj_hook")

    def identity(**kwargs: object) -> None:
        assert ready

    with (
        mock.patch.object(
            native_runtime, "pending", side_effect=lambda: () if ready else ("tj_hook",)
        ),
        mock.patch.object(native_runtime, "prepare", side_effect=prepare) as bootstrap,
        mock.patch.object(
            install.machine_events, "require_tommy", side_effect=identity
        ) as who,
    ):
        steps = install.selected_steps({"native CLI"})
        assert install.run_steps(
            steps, context, check_only, io.StringIO(), io.StringIO()
        ) == int(check_only)
    assert ready is not check_only
    assert bootstrap.call_count == int(not check_only)
    assert who.call_count == int(not check_only)


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
        mock.patch.object(native_runtime, "pending", return_value=()),
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
