import io
import json
import re
import subprocess
from pathlib import Path
from types import ModuleType

import install
import pytest
import work_items_install

ROOT = Path(__file__).resolve().parents[1]
PINS_FILE = ROOT / ".agents/agent/runtime/work-items.json"
PINS = json.loads(PINS_FILE.read_text(encoding="utf-8"))
LOCK_FILE = ROOT / ".agents/agent/utility/plugins/scripts/marketplace_plugins.lock.json"
UV = "uv-executable"
PROFILE_PIN = {
    "package": "tj-example-employer-profile",
    "repository": "tj-agents/example-employer",
    "subdirectory": "packages/tj-example-employer-profile",
    "revision": "a" * 40,
}
PROFILE_IDENTITY = "example-employer@tj-agents"


def requirement(pin: dict[str, str]) -> str:
    return (
        f"git+https://github.com/{pin['repository']}@{pin['revision']}"
        f"#subdirectory={pin['subdirectory']}"
    )


def write_receipt(tools: Path, pins: list[dict[str, str]]) -> None:
    rows = [
        f'{{ name = "{pin["package"]}", git = "https://github.com/{pin["repository"]}'
        f"?subdirectory={pin['subdirectory'].replace('/', '%2F')}"
        f'&rev={pin["revision"]}" }},'
        for pin in pins
    ]
    path = tools / "tj-work-items" / "uv-receipt.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(["[tool]", "requirements = [", *rows, "]", ""])
    path.write_text(text, encoding="utf-8")


class FakeUv:
    def __init__(
        self, tools: Path, profiles: list[dict[str, str]] | None = None
    ) -> None:
        self.tools = tools
        self.profiles = profiles or []
        self.installs: list[list[str]] = []

    def __call__(
        self, command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        assert command[:3] == [UV, "tool", "install"], command
        self.installs.append(command)
        pins = [PINS["tool"], *self.profiles]
        write_receipt(self.tools, [pin for pin in pins if requirement(pin) in command])
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return tmp_path


def install_claude_plugin(
    machine: Path,
    identity: str,
    *,
    declaration: dict[str, object] | None = None,
    revision: str = "a" * 40,
) -> Path:
    """Record `identity` as an enabled Claude plugin with an optional declaration."""
    root = (
        machine
        / "claude"
        / "plugins"
        / "cache"
        / identity.split("@", 1)[1]
        / identity.split("@", 1)[0]
    )
    root.mkdir(parents=True, exist_ok=True)
    if declaration is not None:
        (root / "work-items.json").write_text(json.dumps(declaration), encoding="utf-8")
    settings = machine / "claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    enabled = (
        json.loads(settings.read_text(encoding="utf-8"))
        if settings.is_file()
        else {"enabledPlugins": {}}
    )
    enabled["enabledPlugins"][identity] = True
    settings.write_text(json.dumps(enabled), encoding="utf-8")
    registry = machine / "claude" / "plugins" / "installed_plugins.json"
    rows = (
        json.loads(registry.read_text(encoding="utf-8"))
        if registry.is_file()
        else {"plugins": {}}
    )
    rows["plugins"][identity] = [
        {"scope": "local", "installPath": str(root), "gitCommitSha": revision}
    ]
    registry.write_text(json.dumps(rows), encoding="utf-8")
    return root


def tj_item_step(machine: Path, uv: FakeUv) -> int:
    context = install.Context(
        home=machine,
        environ={"UV_TOOL_DIR": str(uv.tools)},
        windows=False,
        run=uv,
        which=lambda name: UV if name == "uv" else None,
    )
    step = next(item for item in install.STEPS if item.name == "tj item CLI")
    return int(install.run_steps([step], context, False, io.StringIO(), io.StringIO()))


def test_tj_item_installs_the_declared_profile_of_an_installed_plugin(
    machine: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_claude_plugin(
        machine,
        PROFILE_IDENTITY,
        declaration={
            "schema_version": 1,
            "package": PROFILE_PIN["package"],
            "subdirectory": PROFILE_PIN["subdirectory"],
        },
        revision=PROFILE_PIN["revision"],
    )
    repositories = {"example-employer": PROFILE_PIN["repository"]}
    pins = work_items_install.discovered_profiles(
        machine / "claude", repositories=repositories
    )
    assert pins == (work_items_install.Pin(**PROFILE_PIN),)
    lock = machine / "marketplace_plugins.lock.json"
    lock.write_text(
        json.dumps(
            {
                "marketplace": {
                    "plugins": [
                        {"name": "example-employer", "repository": "example-employer"}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(work_items_install, "MARKETPLACE_LOCK", lock)
    uv = FakeUv(machine / "uv-tools", profiles=[PROFILE_PIN])
    assert tj_item_step(machine, uv) == 0
    assert uv.installs == [
        [
            UV,
            "tool",
            "install",
            "--force",
            "--from",
            requirement(PINS["tool"]),
            "--with",
            requirement(PROFILE_PIN),
            "tj-work-items",
        ]
    ]


def test_tj_item_is_a_no_op_when_the_receipt_matches_the_pins(machine: Path) -> None:
    tools = machine / "uv-tools"
    write_receipt(tools, [PINS["tool"]])
    uv = FakeUv(tools)
    assert tj_item_step(machine, uv) == 0
    assert uv.installs == []


def test_tj_item_installs_no_profile_without_a_declared_plugin(
    machine: Path,
) -> None:
    tools = machine / "uv-tools"
    write_receipt(tools, [PINS["tool"]])
    uv = FakeUv(tools)
    assert tj_item_step(machine, uv) == 0
    assert len(uv.installs) == 0


def test_a_plugin_without_a_declaration_selects_no_profile(machine: Path) -> None:
    install_claude_plugin(machine, "personal@tj-agents")
    assert work_items_install.discovered_profiles(machine / "claude") == ()


def test_an_undeclared_marketplace_plugin_raises(machine: Path) -> None:
    install_claude_plugin(
        machine,
        PROFILE_IDENTITY,
        declaration={
            "schema_version": 1,
            "package": PROFILE_PIN["package"],
            "subdirectory": PROFILE_PIN["subdirectory"],
        },
    )
    with pytest.raises(work_items_install.WorkItemsError, match="example-employer"):
        work_items_install.discovered_profiles(machine / "claude", repositories={})


def test_tj_item_fails_without_uv(machine: Path) -> None:
    context = install.Context(
        home=machine,
        environ={},
        windows=False,
        run=FakeUv(machine),
        which=lambda name: None,
    )
    with pytest.raises(install.InstallError, match="uv is required"):
        install.check_tj_item(context)


@pytest.mark.parametrize(
    ("name", "module"),
    [("worktrunk", install.worktrunk_install), ("Handy", install.handy_install)],
)
def test_cli_tool_steps_run_tj_clis_own_installer(
    machine: Path, monkeypatch: pytest.MonkeyPatch, name: str, module: ModuleType
) -> None:
    calls: list[list[str]] = []

    def main(arguments: list[str]) -> int:
        calls.append(arguments)
        print("missing" if arguments == ["--check"] else "installed")
        return 1 if arguments == ["--check"] else 0

    monkeypatch.setattr(module, "main", main)
    context = install.Context(home=machine, environ={}, windows=False)
    step = next(item for item in install.STEPS if item.name == name)
    output = io.StringIO()
    assert install.run_steps([step], context, False, output, io.StringIO()) == 0
    assert calls == [["--check"], []]
    assert f"apply  {name}: installed" in output.getvalue()


def test_repository_names_reads_the_marketplace_snapshot() -> None:
    repositories = work_items_install.repository_names(LOCK_FILE)
    assert repositories["agent"] == "tj-agents/agent"


def test_every_pin_is_matched_by_the_renovate_manager() -> None:
    renovate = json.loads((ROOT / ".github/renovate.json").read_text(encoding="utf-8"))
    path = PINS_FILE.relative_to(ROOT).as_posix()
    managers = [
        manager
        for manager in renovate["customManagers"]
        if any(
            re.search(pattern.strip("/"), path)
            for pattern in manager["managerFilePatterns"]
        )
    ]
    assert len(managers) == 1
    text = PINS_FILE.read_text(encoding="utf-8")
    found = {
        (match["depName"], match["currentDigest"])
        for pattern in managers[0]["matchStrings"]
        for match in re.finditer(pattern.replace("(?<", "(?P<"), text)
    }
    assert found == {(PINS["tool"]["repository"], PINS["tool"]["revision"])}
