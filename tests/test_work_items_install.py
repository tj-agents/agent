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
UV = "uv-executable"


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
    def __init__(self, tools: Path) -> None:
        self.tools = tools
        self.installs: list[list[str]] = []

    def __call__(
        self, command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        assert command[:3] == [UV, "tool", "install"], command
        self.installs.append(command)
        pins = [PINS["tool"], *PINS["profiles"].values()]
        write_receipt(self.tools, [pin for pin in pins if requirement(pin) in command])
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return tmp_path


def install_claude_plugins(machine: Path, *identities: str) -> None:
    registry = machine / "claude" / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    rows = {
        identity: [{"scope": "local", "installPath": "x"}] for identity in identities
    }
    registry.write_text(json.dumps({"plugins": rows}), encoding="utf-8")


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


def test_tj_item_installs_exactly_the_pinned_command_with_selected_profiles(
    machine: Path,
) -> None:
    install_claude_plugins(machine, "agent@tj-agents", *PINS["profiles"])
    uv = FakeUv(machine / "uv-tools")
    assert tj_item_step(machine, uv) == 0
    profiles = [
        argument
        for pin in PINS["profiles"].values()
        for argument in ("--with", requirement(pin))
    ]
    assert uv.installs == [
        [
            UV,
            "tool",
            "install",
            "--force",
            "--from",
            requirement(PINS["tool"]),
            *profiles,
            "tj-work-items",
        ]
    ]


def test_tj_item_is_a_no_op_when_the_receipt_matches_the_pins(machine: Path) -> None:
    install_claude_plugins(machine, *PINS["profiles"])
    tools = machine / "uv-tools"
    write_receipt(tools, [PINS["tool"], *PINS["profiles"].values()])
    uv = FakeUv(tools)
    assert tj_item_step(machine, uv) == 0
    assert uv.installs == []


def test_tj_item_installs_no_profile_the_machine_has_not_selected(
    machine: Path,
) -> None:
    install_claude_plugins(machine, "agent@tj-agents", "personal@tj-agents")
    tools = machine / "uv-tools"
    write_receipt(tools, [PINS["tool"], *PINS["profiles"].values()])
    uv = FakeUv(tools)
    assert tj_item_step(machine, uv) == 0
    assert len(uv.installs) == 1
    assert "--with" not in uv.installs[0]


def test_a_codex_profile_install_selects_its_package(machine: Path) -> None:
    plugin = next(iter(PINS["profiles"]))
    name, _, marketplace = plugin.partition("@")
    (machine / "codex/plugins/cache" / marketplace / name / "local").mkdir(parents=True)
    pins = work_items_install.Pins.load(PINS_FILE)
    assert pins.selected(work_items_install.installed_plugins()) == (
        pins.profiles[plugin],
    )


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
    pins = [PINS["tool"], *PINS["profiles"].values()]
    assert found == {(pin["repository"], pin["revision"]) for pin in pins}
    assert len(found) == len(pins)
