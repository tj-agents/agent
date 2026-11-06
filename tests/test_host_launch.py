import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_cli(runtime: Path) -> ModuleType:
    for name in (
        "agent_cli.py",
        "package_root.py",
        "host.py",
        "launch_errors.py",
        "launch_contract.py",
    ):
        shutil.copy2(ROOT / ".agents/agent/runtime" / name, runtime / name)
    spec = importlib.util.spec_from_file_location(
        "isolated_launch_cli", runtime / "agent_cli.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_host_launch_refuses_missing_nearest_adapter(
    loader_package_roots: tuple[Path, Path], host: str
) -> None:
    outer, inner = loader_package_roots
    runtime = inner / ".agents/agent/runtime"
    runtime.mkdir(parents=True)
    sentinel = outer / ".agents/agent/runtime" / host / "launch.py"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("raise AssertionError('outer host loaded')\n", encoding="utf-8")
    cli = load_cli(runtime)
    with pytest.raises(cli.LaunchError, match="launch adapter was not found under"):
        cli.host_launch(host)


@pytest.mark.parametrize("host", ["codex", "claude"])
@pytest.mark.parametrize("authored", [True, False])
def test_host_launch_uses_only_its_declared_layout(
    tmp_path: Path, host: str, authored: bool
) -> None:
    root = tmp_path / "agent"
    marker = root / (".git" if authored else ".codex-plugin/plugin.json")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")
    runtime = root / ".agents/agent/runtime"
    runtime.mkdir(parents=True)
    adapter = (
        root / f".{host}/runtime/launch.py"
        if authored
        else runtime / host / "launch.py"
    )
    adapter.parent.mkdir(parents=True)
    adapter.write_text("SESSION_ENVIRONMENT = ('owned',)\n", encoding="utf-8")
    cli = load_cli(runtime)
    loaded = cli.host_launch(host)
    assert Path(loaded.__file__) == adapter
    assert loaded.SESSION_ENVIRONMENT == ("owned",)
    assert cli.host_launch(host) is loaded


def test_host_launch_rejects_unknown_host(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("", encoding="utf-8")
    runtime = tmp_path / ".agents/agent/runtime"
    runtime.mkdir(parents=True)
    cli = load_cli(runtime)
    with pytest.raises(ValueError):
        cli.host_launch("other")
