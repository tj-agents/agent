from __future__ import annotations

import tomllib
from pathlib import Path

import install
import pytest
from codex import daemon

DISABLED = """model = "gpt-6"

[features]
js_repl = false
daemon_auto_start = false

[shell_environment_policy.set]
TJ_AGENT = "1"
"""


def test_the_disabling_setting_is_removed_and_nothing_else_changes() -> None:
    enabled = daemon.enabled_text(DISABLED)
    assert enabled == DISABLED.replace("daemon_auto_start = false\n", "")
    assert tomllib.loads(enabled)["features"] == {"js_repl": False}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[features]\njs_repl = false\n",
        "[features]\ndaemon_auto_start = true\n",
        "[profiles.work.features]\ndaemon_auto_start = false\n",
    ],
)
def test_config_that_does_not_disable_the_daemon_is_left_alone(text: str) -> None:
    assert daemon.enabled_text(text) == text


def test_a_dotted_root_setting_is_removed() -> None:
    text = 'features.daemon_auto_start = false\nmodel = "gpt-6"\n'
    assert daemon.enabled_text(text) == 'model = "gpt-6"\n'


def test_crlf_config_keeps_its_line_endings() -> None:
    text = DISABLED.replace("\n", "\r\n")
    assert daemon.enabled_text(text) == text.replace(
        "daemon_auto_start = false\r\n", ""
    )


def test_the_install_step_reports_drift_then_converges(tmp_path: Path) -> None:
    context = install.Context(
        home=tmp_path, environ={}, windows=False, documents=tmp_path / "Documents"
    )
    config = install.codex_config_path(context)
    config.parent.mkdir(parents=True)
    config.write_text(DISABLED, encoding="utf-8")
    assert install.check_codex_daemon(context) == [
        f"let Codex launches start its shared daemon in {config}"
    ]
    assert install.apply_codex_daemon(context) == [
        f"removed daemon_auto_start = false from {config}"
    ]
    assert install.check_codex_daemon(context) == []
    assert "daemon_auto_start" not in config.read_text(encoding="utf-8")
    assert install.apply_codex_daemon(context) == []
