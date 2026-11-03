import importlib
import json
import shutil
from pathlib import Path


def test_clean_package_installs_runtime_without_retired_close_export(
    tmp_path: Path,
) -> None:
    install = importlib.import_module("install")
    package = tmp_path / "package"
    for source in Path(install.ROOT).rglob("*"):
        if source.is_file():
            target = package / source.relative_to(install.ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    retired = Path(".agents/agent/utility/close/scripts")
    assert not (package / retired).exists()
    receipt = install.runtime_receipt(package)
    assert retired.as_posix() not in receipt["exports"]
    assert receipt["files"]
    installed = tmp_path / "installed"
    script = install.install_runtime(installed, package)
    shutil.rmtree(package)
    assert script.is_file()
    assert not (installed / retired).exists()
    assert (
        json.loads((installed / "source.json").read_text(encoding="utf-8")) == receipt
    )
    for relative, expected in receipt["files"].items():
        assert (
            install.hashlib.sha256((installed / relative).read_bytes()).hexdigest()
            == expected
        )
