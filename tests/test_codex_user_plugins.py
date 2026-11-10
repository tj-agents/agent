from __future__ import annotations

import pytest
from codex import user_plugins

CONFIG = """model = "gpt-6"

[plugins."base@tj-agents"]
enabled = true

[plugins."agent@tj-agents"]
enabled = true

[features]
js_repl = false
"""


def test_enabled_lists_user_enabled_plugins() -> None:
    assert user_plugins.enabled(CONFIG) == {"base@tj-agents", "agent@tj-agents"}


def test_only_the_named_plugin_tables_are_removed() -> None:
    assert user_plugins.without(CONFIG, ["agent@tj-agents"]) == CONFIG.replace(
        '[plugins."agent@tj-agents"]\nenabled = true\n\n', ""
    )


@pytest.mark.parametrize("identities", [[], ["cli@tj-agents"]])
def test_absent_plugins_leave_the_config_unchanged(identities: list[str]) -> None:
    assert user_plugins.without(CONFIG, identities) == CONFIG
