import json
import os
import runpy
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import agent_cli
import pytest
from codex import launch as codex_launch
from codex import plugins as codex_plugins
from codex import startup
from fixtures import foreground_console
from launch_contract import PreparedLaunch
from launch_errors import LaunchError
from tj_agent.host import Host

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".agents/agent/runtime"
type Main = Callable[..., int]


def command_main() -> Main:
    source = runpy.run_path(str(RUNTIME / "host_command.py"))
    return cast(Main, source["main"])


def completed(
    command: Sequence[str], code: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(command), code, "", "")


def test_native_codex_maintenance_can_upgrade_an_older_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = str(tmp_path / "codex.exe")
    monkeypatch.setattr(
        agent_cli.host_launch(Host.CODEX), "candidate_paths", lambda: [native]
    )
    monkeypatch.setattr(
        codex_launch.process,
        "run",
        lambda command, **options: subprocess.CompletedProcess(
            command, 0, "codex-cli 0.163.0", ""
        ),
    )
    calls: list[Sequence[str]] = []

    def run(
        command: Sequence[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return completed(command)

    main = command_main()
    assert main(Host.CODEX, ("update",), cwd=tmp_path, environ={}, run=run) == 0
    assert calls == [[native, "update"]]
    assert main(Host.CODEX, (), cwd=tmp_path, environ={}, run=run) == 1
    assert calls == [[native, "update"]]


@pytest.mark.parametrize(
    ("cli_version", "daemon_status", "package_version", "expected"),
    [
        ("0.168.0", "running", "0.170.0", "managed"),
        ("0.169.0", "running", "0.170.0", "managed"),
        ("0.171.0", "running", "0.170.0", "managed"),
        ("0.171.0", "stopped", None, "newest"),
        ("0.171.0", "running", "0.169.0", None),
    ],
)
def test_managed_codex_matches_the_running_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli_version: str,
    daemon_status: str,
    package_version: str | None,
    expected: str | None,
) -> None:
    newest = str(tmp_path / "path" / "codex.exe")
    managed = str(tmp_path / "daemon" / "codex.exe")

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        if command == [newest, "app-server", "daemon", "version"]:
            content = json.dumps(
                {
                    "status": daemon_status,
                    "appServerVersion": "0.170.0",
                    "managedCodexPath": managed,
                }
            )
        else:
            installed = {newest: cli_version, managed: package_version}
            assert command[1:] == ["--version"]
            content = f"codex-cli {installed[command[0]]}"
        return subprocess.CompletedProcess(command, 0, content, "")

    monkeypatch.setattr(codex_launch.process, "run", run)
    monkeypatch.setattr(
        agent_cli.host_launch(Host.CODEX), "candidate_paths", lambda: [newest]
    )
    assert codex_plugins.resolve_executable(maintenance=True) == newest
    if cli_version == "0.171.0":
        assert agent_cli.host_launch(Host.CODEX).executable() == newest
    if expected is None:
        with pytest.raises(LaunchError, match=r"daemon is running 0\.170\.0"):
            codex_plugins.resolve_executable()
    else:
        assert (
            codex_plugins.resolve_executable()
            == {
                "managed": managed,
                "newest": newest,
            }[expected]
        )


@pytest.mark.parametrize("stale_socket", [False, True])
def test_fresh_managed_launch_starts_and_verifies_the_selected_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_socket: bool
) -> None:
    newest = str(tmp_path / "codex")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    if stale_socket:
        socket = tmp_path / "app-server-control/app-server-control.sock"
        socket.parent.mkdir()
        socket.touch()
    inherited = (
        "TJ_RUN_PID",
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "WT_SESSION",
        "AGENT_CLI_TAB_TITLE",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_PID",
        "WORKBOARD_LAUNCH_TOKEN",
        "WORKBOARD_WORKFLOW_TOKEN",
    )
    for name in inherited:
        monkeypatch.setenv(name, "caller")
    adapter = agent_cli.host_launch(Host.CODEX)
    monkeypatch.setattr(adapter, "candidate_paths", lambda: [newest])
    events: list[str] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        assert command[0] == newest
        operation = command[-1]
        events.append(operation)
        if operation == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 0.171.0", "")
        if operation == "start":
            environment = cast(dict[str, str], options["env"])
            assert environment["CODEX_HOME"] == str(tmp_path)
            assert all(name not in environment for name in inherited)
            return subprocess.CompletedProcess(command, 0, "", "")
        assert command[1:] == ["app-server", "daemon", "version"]
        assert options["encoding"] == "utf-8"
        if "start" in events:
            reply = json.dumps({"status": "running", "appServerVersion": "0.171.0"})
            return subprocess.CompletedProcess(command, 0, reply, "")
        return subprocess.CompletedProcess(
            command, 1, "", "failed to connect to socket (os error 10061)"
        )

    monkeypatch.setattr(codex_launch.process, "run", run)
    monkeypatch.setattr(codex_launch.subprocess, "run", run)
    assert codex_plugins.resolve_executable(start_if_absent=True) == newest
    assert events == ["--version", "version", "start", "version"]


@pytest.mark.parametrize("host", tuple(Host))
@pytest.mark.parametrize("parent", [None, "claude:requested"])
def test_prepared_tab_passes_only_its_requested_relationship_to_the_wrapper(
    tmp_path: Path, host: Host, parent: str | None
) -> None:
    native = str(tmp_path / "native")
    metadata = ["--title", "Named tab", "--cwd", str(tmp_path), "--launch-id", "new"]
    if parent:
        metadata.extend(("--parent", parent))
    prompt = "Read my prepared file."
    calls: list[Sequence[str]] = []

    def run(
        command: Sequence[str], *, env: Mapping[str, str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        assert not env
        calls.append(command)
        return completed(command)

    result = command_main()(
        host,
        ["--tj-tab", *metadata, "--", native, "--", prompt],
        cwd=tmp_path,
        environ={
            "TJ_RUN_LAUNCH_ID": "prior",
            "CLAUDE_CODE_SESSION_ID": "prior",
            "CODEX_THREAD_ID": "prior",
        },
        resolver=lambda: native,
        run=run,
    )
    assert result == 0
    (call,) = calls
    assert call[: call.index("--")] == [
        "tj",
        "_session",
        "--host",
        host.value,
        *metadata,
    ]
    assert call[-2:] == ["--", prompt]


def test_exact_resume_checks_the_selected_executable_and_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Mock()
    server.request.return_value = {"data": [], "nextCursor": None}
    connect = Mock(return_value=nullcontext(server))
    probe = Mock(return_value=subprocess.CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr(codex_launch.processes, "inventory", dict)
    monkeypatch.setattr(codex_launch.process, "run", probe)
    monkeypatch.setattr(codex_launch, "daemon", connect)
    codex_launch.require_stopped_thread("fixture", "selected-native")
    assert probe.call_args.args[0][0] == "selected-native"
    connect.assert_called_once_with(native="selected-native")


def test_native_foreground_command_keeps_the_calling_console(tmp_path: Path) -> None:
    receipt = tmp_path / "console.json"
    fixture = Path(__file__).with_name("fixtures") / "foreground_console.py"
    before = foreground_console.identity()
    code = command_main()(
        Host.CODEX,
        (str(fixture), str(receipt), "--help"),
        cwd=tmp_path,
        environ={},
        resolver=lambda: sys.executable,
    )
    assert code == 0
    after = json.loads(receipt.read_text(encoding="utf-8"))
    if os.name == "nt" and before:
        assert os.getpid() in after
    else:
        assert after == before


def test_managed_command_preserves_native_arguments_and_exit_code(
    tmp_path: Path,
) -> None:
    main = command_main()
    calls: list[tuple[Sequence[str], dict[str, object]]] = []
    arguments = ("--model", "native model", "prompt & | < > ^ %")
    environment = {
        "CLAUDE_CONFIG_DIR": "configured",
        "CODEX_HOME": "also-configured",
        "TJ_RUN_LAUNCH_ID": "parent-launch",
        "TJ_RUN_ITEM": "parent-item",
        "CLAUDE_CODE_SESSION_ID": "parent-session",
        "CODEX_THREAD_ID": "other-session",
    }

    def run(
        values: Sequence[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((values, options))
        return completed(values, 23 if values[1:2] == ["_session"] else 0)

    result = main(
        Host.CLAUDE,
        arguments,
        cwd=tmp_path,
        environ=environment,
        resolver=lambda: str(tmp_path / "native host"),
        run=run,
    )

    assert result == 23
    (session,) = calls
    assert session[0] == [
        "tj",
        "_session",
        "--host",
        "claude",
        "--cwd",
        str(tmp_path.resolve()),
        "--command-cwd",
        str(tmp_path.resolve()),
        "--",
        str((tmp_path / "native host").resolve()),
        *arguments,
    ]
    for _values, options in calls:
        child = cast(dict[str, str], options["env"])
        assert child["CLAUDE_CONFIG_DIR"] == "configured"
        assert child["CODEX_HOME"] == "also-configured"
        assert "TJ_RUN_LAUNCH_ID" not in child
        assert "TJ_RUN_ITEM" not in child
        assert "CLAUDE_CODE_SESSION_ID" not in child
        assert "CODEX_THREAD_ID" not in child


@pytest.mark.parametrize("prompt", [(), ("--", "--no-daemon")])
def test_managed_codex_prebinds_the_exact_thread_in_the_effective_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompt: tuple[str, ...],
) -> None:
    main = command_main()
    target = tmp_path / "café"
    target.mkdir()
    calls: list[tuple[Sequence[str], dict[str, object]]] = []
    thread = "01234567-89ab-4cde-8123-0123456789ab"

    def prepare(
        directory: Path,
        title: str,
        arguments: list[str],
        cli: str,
        native: str,
        environment: Mapping[str, str],
    ) -> PreparedLaunch:
        assert directory == target
        assert arguments == ["--cd", str(target), *prompt[1:]]
        return PreparedLaunch(
            (
                native,
                "--cd",
                str(target),
                "resume",
                thread,
                "--remote",
                "unix://",
                *prompt,
            ),
            f"codex:{thread}",
            created=True,
        )

    monkeypatch.setattr(startup, "prepare", prepare)

    def run(
        values: Sequence[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((values, options))
        return completed(values)

    result = main(
        Host.CODEX,
        ("-C", "café", *prompt),
        cwd=tmp_path,
        environ={},
        resolver=lambda: str(tmp_path / "codex-native"),
        run=run,
    )

    assert result == 0
    (call,) = calls
    command, options = call
    assert command[command.index("--cwd") + 1] == str(target.resolve())
    assert command[command.index("--command-cwd") + 1] == str(tmp_path.resolve())
    assert options["cwd"] == tmp_path.resolve()
    assert command[command.index("--session") + 1] == f"codex:{thread}"
    assert command[command.index("--") + 2 :] == [
        "--cd",
        str(target),
        "resume",
        thread,
        "--remote",
        "unix://",
        *prompt,
    ]


@pytest.mark.parametrize(
    ("host", "arguments"),
    [
        (Host.CLAUDE, ("auth", "login")),
        (Host.CODEX, ("--help",)),
        (Host.CLAUDE, ("agents", "--json")),
        (Host.CODEX, ("app-server", "daemon", "version")),
    ],
)
def test_native_maintenance_commands_bypass_preparation_and_session_wrapper(
    tmp_path: Path, host: Host, arguments: tuple[str, ...]
) -> None:
    main = command_main()
    calls: list[tuple[Sequence[str], dict[str, object]]] = []

    def run(
        values: Sequence[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((values, options))
        return completed(values, 17)

    result = main(
        host,
        arguments,
        cwd=tmp_path,
        environ={},
        resolver=lambda: str(tmp_path / "native"),
        run=run,
    )

    assert result == 17
    assert calls == [
        (
            [str((tmp_path / "native").resolve()), *arguments],
            {"cwd": tmp_path, "env": {}, "check": False},
        )
    ]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            ("--remote", "unix://", "prompt"),
            ("--remote", "unix://", "prompt"),
        ),
        (
            ("--", "--no-daemon"),
            ("--remote", "unix://", "--", "--no-daemon"),
        ),
        (
            ("--", "--remote=literal"),
            (
                "--remote",
                "unix://",
                "--",
                "--remote=literal",
            ),
        ),
        (
            ("--", "--dangerously-bypass-hook-trust"),
            (
                "--remote",
                "unix://",
                "--",
                "--dangerously-bypass-hook-trust",
            ),
        ),
        (
            ("--remote=unix://", "prompt"),
            ("--remote=unix://", "prompt"),
        ),
    ],
)
def test_managed_codex_keeps_explicit_flags_and_literal_prompts(
    arguments: tuple[str, ...],
    expected: tuple[str, ...],
) -> None:
    from codex import launch

    assert launch.managed_arguments(arguments) == expected


@pytest.mark.parametrize(
    "arguments",
    [
        ("--remote", "unix:///different.sock"),
        ("resume", "--remote=wss://example.invalid"),
        ("--no-daemon",),
        ("--dangerously-bypass-hook-trust",),
        ("--remote-auth-token-env", "TOKEN"),
        ("--remote",),
    ],
)
def test_uncontrollable_codex_refuses_before_starting_a_managed_wrapper(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    calls: list[Sequence[str]] = []

    def run(
        values: Sequence[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(values)
        return completed(values)

    assert (
        command_main()(
            Host.CODEX,
            arguments,
            cwd=tmp_path,
            environ={},
            resolver=lambda: str(tmp_path / "native"),
            run=run,
        )
        == 1
    )
    assert not calls


@pytest.mark.parametrize("host", ("claude", "codex"))
def test_packaged_host_entry_loads_only_its_bundled_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    package = tmp_path / "package"
    (package / ".codex-plugin").mkdir(parents=True)
    (package / ".codex-plugin/plugin.json").write_text("{}", encoding="utf-8")
    runtime = package / ".agents/agent/runtime"
    adapter = runtime / host / "command.py"
    adapter.parent.mkdir(parents=True)
    shutil.copy2(ROOT / f".{host}/runtime/command.py", adapter)
    (runtime / "tj_agent").mkdir()
    (runtime / "tj_agent/__init__.py").write_text("", encoding="utf-8")
    (runtime / "tj_agent/host.py").write_text(
        "from enum import Enum\n"
        "class Host(Enum):\n"
        " CLAUDE = 'claude'\n"
        " CODEX = 'codex'\n",
        encoding="utf-8",
    )
    (runtime / "host_command.py").write_text(
        "import json\n"
        "def main(host, arguments, *, cwd, environ):\n"
        " print(json.dumps({'host': host.value, 'arguments': arguments,\n"
        " 'cwd': str(cwd), 'environment': environ['TJ_COMMAND_TEST']}))\n"
        " return 0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TJ_COMMAND_TEST", "fixture environment")
    arguments = ["--model", "native model", "prompt & | café"]

    result = subprocess.run(
        [sys.executable, "-E", "-s", "-B", str(adapter), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "host": host,
        "arguments": arguments,
        "cwd": str(tmp_path),
        "environment": "fixture environment",
    }
