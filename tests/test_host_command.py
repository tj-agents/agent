import json
import os
import runpy
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

import pytest
from fixtures import foreground_console
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


def test_native_foreground_command_keeps_the_calling_console(tmp_path: Path) -> None:
    receipt = tmp_path / "console.json"
    fixture = Path(__file__).with_name("fixtures") / "foreground_console.py"
    before = foreground_console.identity()
    code = command_main()(
        Host.CODEX,
        (str(fixture), str(receipt), "--help"),
        cwd=tmp_path,
        environ=os.environ,
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


def test_managed_command_uses_the_hosts_effective_directory(tmp_path: Path) -> None:
    main = command_main()
    target = tmp_path / "café"
    target.mkdir()
    calls: list[tuple[Sequence[str], dict[str, object]]] = []

    def run(
        values: Sequence[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((values, options))
        return completed(values)

    result = main(
        Host.CODEX,
        ("-C", "café", "prompt"),
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
    assert command[-3:] == ["-C", "café", "prompt"]
    assert command[command.index("--") + 2] == "--no-daemon"


@pytest.mark.parametrize(
    ("host", "arguments"),
    [(Host.CLAUDE, ("auth", "login")), (Host.CODEX, ("--help",))],
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
        (("--no-daemon", "prompt"), ("--no-daemon", "prompt")),
        (("--", "--no-daemon"), ("--no-daemon", "--", "--no-daemon")),
        (("--", "--remote=literal"), ("--no-daemon", "--", "--remote=literal")),
    ],
)
def test_managed_codex_keeps_explicit_flags_and_literal_prompts(
    arguments: tuple[str, ...],
    expected: tuple[str, ...],
) -> None:
    from codex import launch

    assert launch.managed_arguments(arguments) == expected


@pytest.mark.parametrize(
    "arguments", [("--remote", "unix://"), ("resume", "--remote=wss://example.invalid")]
)
def test_remote_codex_refuses_before_starting_a_managed_wrapper(
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
