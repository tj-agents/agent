import json
import os
import runpy
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

import agent_cli
import pytest
from codex import launch as codex_launch
from codex import startup
from fixtures import foreground_console
from launch_contract import PreparedLaunch
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


@pytest.mark.parametrize(
    ("host", "windows", "terminal", "force_colour", "redirected"),
    [
        (Host.CODEX, True, None, "0", None),
        (Host.CODEX, False, "screen-256color", "0", None),
        (Host.CLAUDE, True, "xterm-256color", "1", None),
        (Host.CLAUDE, False, "screen-256color", "1", None),
        (Host.CLAUDE, True, "dumb", "0", "stdout"),
        (Host.CLAUDE, False, "screen-256color", "0", "stdin"),
    ],
)
def test_managed_foreground_uses_the_tab_colour_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: Host,
    windows: bool,
    terminal: str | None,
    force_colour: str,
    redirected: str | None,
) -> None:
    parent = {
        "NO_COLOR": "1",
        "FORCE_COLOR": "0",
        "TERM": "dumb" if windows else "screen-256color",
        "KEPT": "value",
    }
    native = str(tmp_path / "provider")
    prepared_environments: list[Mapping[str, str]] = []
    child_environments: list[Mapping[str, str]] = []

    def prepare(
        executable: str,
        _directory: Path,
        arguments: Sequence[str],
        _cli: str,
        environment: Mapping[str, str],
    ) -> PreparedLaunch:
        prepared_environments.append(dict(environment))
        return PreparedLaunch((executable, *arguments))

    def run(
        command: Sequence[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        child_environments.append(cast(Mapping[str, str], options["env"]))
        return completed(command)

    monkeypatch.setattr(agent_cli, "IS_WINDOWS", windows)
    monkeypatch.setattr(agent_cli.host_launch(host), "prepare_foreground", prepare)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: redirected != "stdin")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: redirected != "stdout")
    main = command_main()
    arguments = ("-p", "prompt") if redirected else ()
    assert (
        main(
            host,
            arguments,
            cwd=tmp_path,
            environ=parent,
            resolver=lambda: native,
            run=run,
        )
        == 0
    )

    assert prepared_environments == child_environments
    (child,) = child_environments
    assert child.get("NO_COLOR") == ("1" if redirected else None)
    assert child.get("TERM") == terminal
    assert child.get("FORCE_COLOR") == force_colour
    assert child["KEPT"] == "value"
    assert parent["NO_COLOR"] == "1"
    assert parent["TERM"] == ("dumb" if windows else "screen-256color")

    assert (
        main(
            host,
            ("--version",),
            cwd=tmp_path,
            environ=parent,
            resolver=lambda: native,
            run=run,
        )
        == 0
    )
    assert child_environments[-1] == parent


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
