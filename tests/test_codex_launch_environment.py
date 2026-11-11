import shutil
import subprocess
from pathlib import Path

import agent_cli
import pytest
from tj_agent.host import Host


@pytest.mark.parametrize("windows", [True, False])
def test_codex_launch_removes_inherited_monochrome_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, windows: bool
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    adapter = agent_cli.host_launch(Host.CODEX)
    monkeypatch.setattr(adapter, "executable", lambda: "codex")
    monkeypatch.setattr(agent_cli, "IS_WINDOWS", windows)
    monkeypatch.setattr(shutil, "which", lambda name: name)
    monkeypatch.setattr(agent_cli, "await_launch", lambda *args, **kwargs: {})
    parent_environment = {
        "NO_COLOR": "1",
        "TERM": "dumb" if windows else "screen-256color",
        "TMUX": "/tmp/test-tmux",
        "KEPT": "value",
    }

    # Exercise the untyped legacy launcher, including its real environment handling.
    agent_cli.launch(  # type: ignore[no-untyped-call]
        Host.CODEX,
        tmp_path,
        "Native Codex handoff",
        ["resume", "existing-session"],
        environ=parent_environment,
        run=run,
    )

    command, options = calls[0]
    assert command[-3:] == ["codex", "resume", "existing-session"]
    if windows:
        child_environment = options["env"]
        assert isinstance(child_environment, dict)
        assert "NO_COLOR" not in child_environment
        assert "TERM" not in child_environment
        assert "FORCE_COLOR" not in child_environment
        assert child_environment["KEPT"] == "value"
    else:
        environment_arguments = command[command.index("env") + 1 : command.index("tj")]
        no_colour = environment_arguments.index("NO_COLOR")
        assert environment_arguments[no_colour - 1] == "-u"
        assert "TERM" not in environment_arguments
        assert not any(value.startswith("TERM=") for value in environment_arguments)
    assert parent_environment["NO_COLOR"] == "1"
    assert parent_environment["TERM"] == ("dumb" if windows else "screen-256color")
