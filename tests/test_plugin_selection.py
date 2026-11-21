import lock_io
import plugin_selection
import pytest


@pytest.mark.parametrize(
    "repository", ["Example-Org/plugins", "alice/tools", "x/a.b_c-d"]
)
def test_custom_marketplace_accepts_any_well_formed_owner(repository: str) -> None:
    source = {"repository": repository}
    assert plugin_selection.validate_marketplace("custom", source) == source


@pytest.mark.parametrize(
    "repository",
    [
        "Example-Org/plugins.git",
        "Example-Org/plugins.GIT",
        "https://github.com/Example-Org/plugins",
        "Example-Org/plugins/extra",
        "Example Org/plugins",
        "Example-Org/../plugins",
        "Example-Org/",
        "/plugins",
    ],
)
def test_custom_marketplace_rejects_malformed_sources(repository: str) -> None:
    with pytest.raises(lock_io.LockError, match=r"without \.git"):
        plugin_selection.validate_marketplace("custom", {"repository": repository})


@pytest.mark.parametrize(
    ("name", "repository"),
    [
        ("tj-agents", "Example-Org/plugins"),
        ("tj-agents", "TJ-Agents/Marketplace"),
        ("custom", "tj-agents/marketplace"),
        ("custom", "TJ-Agents/Marketplace"),
        ("custom", "tj-agents/agent"),
        ("custom", "TJ-Agents/Core"),
    ],
)
def test_reserved_marketplace_identity_and_producer_exclusions_remain(
    name: str, repository: str
) -> None:
    with pytest.raises(lock_io.LockError, match="tj-agents marketplace"):
        plugin_selection.validate_marketplace(name, {"repository": repository})


def test_marketplace_sources_remain_unique_ignoring_case() -> None:
    document = {
        "schema_version": 1,
        "marketplaces": {
            "first": {"repository": "Example-Org/plugins"},
            "second": {"repository": "example-org/Plugins"},
        },
        "plugins": ["tool@first", "tool@second"],
    }
    with pytest.raises(lock_io.LockError, match="same repository"):
        plugin_selection.validate(document)


def test_plugin_identities_remain_unique() -> None:
    document = {
        "schema_version": 1,
        "marketplaces": {"custom": {"repository": "Example-Org/plugins"}},
        "plugins": ["tool@custom", "tool@custom"],
    }
    with pytest.raises(lock_io.LockError, match="must not repeat"):
        plugin_selection.validate(document)
