import ast
import re
from pathlib import Path, PurePosixPath

import capabilities
import pytest
from tj_agent.host import Host

ROOT = Path(__file__).resolve().parents[1]


def test_registry_resolves_all_agent_implementations_and_contracts() -> None:
    assert (
        capabilities.validate({"agent": capabilities.Source(ROOT)}, local_only=True)
        == []
    )


@pytest.mark.parametrize("host", list(Host))
def test_a_missing_host_contract_is_rejected(host: Host) -> None:
    with pytest.raises(ValueError, match="implementation and contract"):
        capabilities.Support(
            implementation=f"agent:.{host}/runtime/launch.py::start_arguments"
        )


def test_an_explicit_native_host_limitation_is_accepted() -> None:
    support = capabilities.Support(
        reason="Codex does not provide native background-session respawn."
    )
    assert support.references() == ()


@pytest.mark.parametrize("reason", [" ", "todo", "not applicable"])
def test_a_placeholder_host_exception_is_rejected(reason: str) -> None:
    with pytest.raises(ValueError, match="concrete reason"):
        capabilities.Support(reason=reason)


@pytest.mark.parametrize(
    "reference",
    [
        "agent:../outside.py::run",
        "agent:/outside.py::run",
        "agent:code.py",
        "missing",
    ],
)
def test_invalid_source_references_are_rejected(reference: str) -> None:
    with pytest.raises(ValueError, match="Invalid capability reference"):
        capabilities.Reference.parse(reference)


def test_deleted_implementations_and_tests_fail_the_check(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("def present(): pass\n", encoding="utf-8")
    support = capabilities.Support(
        "agent:code.py::deleted", "agent:code.py::test_missing"
    )
    entries = (capabilities.Capability("fixture", support, support),)
    errors = capabilities.validate({"agent": capabilities.Source(tmp_path)}, entries)
    assert len(errors) == 4
    assert all("symbol does not exist" in error for error in errors)
    assert any("/claude:" in error for error in errors)
    assert any("/codex:" in error for error in errors)


def test_a_missing_external_snapshot_fails_without_local_mode() -> None:
    errors = capabilities.validate({"agent": capabilities.Source(ROOT)})
    assert any("source was not supplied" in error for error in errors)


def test_an_external_snapshot_must_match_the_declared_pin(tmp_path: Path) -> None:
    errors = capabilities.validate(
        {
            "agent": capabilities.Source(ROOT),
            "cli": capabilities.Source(tmp_path, "0" * 40),
        }
    )
    assert any("source does not match its pinned revision" in error for error in errors)


def test_every_external_repository_has_an_immutable_revision() -> None:
    assert all(
        re.fullmatch(r"[0-9a-f]{40}", revision)
        for revision in capabilities.REVISIONS.values()
    )


def test_duplicate_capability_names_are_rejected() -> None:
    entry = capabilities.CAPABILITIES[0]
    errors = capabilities.validate({"agent": capabilities.Source(ROOT)}, (entry, entry))
    assert "Capability names must be nonempty and unique" in errors


def test_python_method_references_are_qualified() -> None:
    nodes = ast.parse("class Suite:\n def test_case(self): pass\n").body
    assert capabilities.python_symbols(nodes) == {"Suite", "Suite.test_case"}


def test_rust_contracts_require_a_test_attribute(tmp_path: Path) -> None:
    (tmp_path / "code.rs").write_text("fn helper() {}\n", encoding="utf-8")
    support = capabilities.Support("agent:code.rs::helper", "agent:code.rs::helper")
    entry = capabilities.Capability("fixture", support, support)
    errors = capabilities.validate({"agent": capabilities.Source(tmp_path)}, (entry,))
    assert len(errors) == 2
    assert all("symbol does not exist" in error for error in errors)


def test_rust_test_attributes_allow_platform_guards() -> None:
    source = "#[cfg(windows)]\n#[test]\nfn contract() {}\nfn helper() {}\n"
    assert capabilities.rust_tests(source) == {"contract"}


@pytest.mark.parametrize(
    "source",
    [
        "// #[test]\nfn helper() {}",
        "/* #[test]\nfn helper() {} */",
        "/* outer /* inner */ #[test]\nfn helper() {} */",
        'const EXAMPLE: &str = r#"#[test]\nfn helper() {}"#;',
        'const EXAMPLE: &str = "#[test]\\nfn helper() {}";',
    ],
)
def test_rust_comments_and_examples_cannot_supply_contracts(source: str) -> None:
    assert capabilities.rust_tests(source) == set()


def test_rust_character_literals_preserve_real_test_attributes() -> None:
    source = (
        'const QUOTE: char = \'"\';\n#[test]\nfn real_test() {}\nconst S: &str = "x";'
    )
    assert capabilities.rust_tests(source) == {"real_test"}


@pytest.mark.parametrize("host", list(Host))
def test_every_launch_protocol_method_has_a_registered_contract(host: Host) -> None:
    source = (ROOT / ".agents/agent/runtime/launch_contract.py").read_text(
        encoding="utf-8"
    )
    required = {
        symbol.removeprefix("Launch.")
        for symbol in capabilities.python_symbols(ast.parse(source).body)
        if symbol.startswith("Launch.")
    }
    covered = {
        reference.symbol
        for capability in capabilities.CAPABILITIES
        for reference in capability.support(host).references()
        if str(reference.path) == f".{host}/runtime/launch.py"
    }
    if host is Host.CODEX:
        background = next(
            entry
            for entry in capabilities.CAPABILITIES
            if entry.name == "background-respawn"
        )
        assert background.support(host).reason
        covered.add("respawn")
    assert not required - covered


def test_source_reference_cannot_follow_a_link_outside_its_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes its repository"):
        capabilities.Source(tmp_path).read(PurePosixPath("../outside.py"))
