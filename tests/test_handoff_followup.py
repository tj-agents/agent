import contextlib
import io
import json
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any
from unittest import mock

import pytest
import test_codex_followup as followup
import test_required_plugins as required_plugins


@pytest.fixture
def inventory() -> Iterator[Any]:
    with ExitStack() as stack:
        fixture: Any = required_plugins.RequiredPluginInventoryTests()
        fixture.addCleanup = stack.callback
        fixture.setUp()
        yield fixture


@pytest.fixture
def caller() -> Iterator[Any]:
    with ExitStack() as stack:
        fixture: Any = followup.FollowupTests()
        fixture.addCleanup = stack.callback
        fixture.setUp()
        yield fixture


def test_handoff_title_override_preserves_required_plugin_verification(
    inventory: Any,
) -> None:
    inventory.select("agent@tj-agents")
    entry = inventory.entry("agent@tj-agents")
    options = (
        "--config",
        "model_reasoning_effort=high",
        "--config",
        "tui.terminal_title=[]",
    )
    with (
        mock.patch("managed_admission.verify") as admission,
        mock.patch.object(
            required_plugins.REQUIRED.process,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({"installed": [entry]}), ""
            ),
        ) as run,
    ):
        project, roots = required_plugins.REQUIRED.check_launch_roots(
            inventory.project,
            required_plugins.Host.CODEX,
            "native-codex",
            [*options, "prepared handoff"],
        )
    assert project == inventory.project.resolve()
    assert roots.keys() == {"agent@tj-agents"}
    admission.assert_called_once_with(
        project,
        required_plugins.Host.CODEX,
        "native-codex",
        native_project=inventory.project.resolve(),
    )
    assert run.call_args.args[0] == [
        "native-codex",
        *options,
        "plugin",
        "list",
        "--marketplace",
        "tj-agents",
        "--json",
    ]


@pytest.mark.parametrize(
    "option",
    [
        ("--config", "plugins.agent.enabled=false"),
        ("--config", "features.hooks=false"),
        ("--profile", "other"),
        ("--disable", "hooks"),
        ("--config", "tui={}"),
    ],
)
def test_title_override_does_not_admit_plugin_activation_overrides(
    inventory: Any, option: tuple[str, str]
) -> None:
    inventory.select("agent@tj-agents")
    with mock.patch.object(required_plugins.REQUIRED.process, "run") as run:
        with pytest.raises(
            required_plugins.REQUIRED.RequiredPluginsError, match="launch override"
        ):
            required_plugins.REQUIRED.check_launch_roots(
                inventory.project,
                required_plugins.Host.CODEX,
                "native-codex",
                ["--config", "tui.terminal_title=[]", *option, "prepared handoff"],
            )
        run.assert_not_called()


def test_claude_caller_delivers_to_explicit_codex_profile_and_thread(
    caller: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = followup.Session(caller.receipt)
    output = io.StringIO()
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "caller")
    monkeypatch.setenv("CODEX_HOME", str(caller.root / "other-profile"))
    with (
        mock.patch.object(followup.FOLLOWUP, "Proxy", return_value=proxy) as connection,
        mock.patch.object(followup.FOLLOWUP, "responsibility", return_value="owner"),
        contextlib.redirect_stdout(output),
    ):
        code = followup.FOLLOWUP.main(caller.arguments())
    assert code == 0
    assert json.loads(output.getvalue())["status"] == "delivered"
    assert followup.FOLLOWUP.path_key(caller.root) == followup.FOLLOWUP.path_key(
        connection.call_args.args[1]
    )
    receipt = json.loads(caller.receipt_path.read_text(encoding="utf-8"))
    assert receipt["thread_id"] == followup.THREAD
    assert sum(method.startswith("turn/") for method, _ in proxy.calls) == 1
