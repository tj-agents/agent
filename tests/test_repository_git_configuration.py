import io
import os
import subprocess
from functools import partial
from pathlib import Path

import install
import pytest


@pytest.fixture
def repository_configuration(
    tmp_path: Path,
) -> tuple[install.Context, Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "selected repository ü"
    global_config = home / ".gitconfig"
    global_config.write_text("[credential]\nhelper = store\n", encoding="utf-8")
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "USERPROFILE": str(home),
        "GIT_CONFIG_GLOBAL": str(global_config),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    run = partial(subprocess.run, env=environment)
    run(
        ["git", "init", "--quiet", str(project)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    selection = project / ".agents/plugins.json"
    selection.parent.mkdir()
    source = Path(__file__).resolve().parents[1] / ".agents/plugins.json"
    selection.write_bytes(source.read_bytes())
    context = install.Context(
        home=home, environ=environment, windows=True, project=project, run=run
    )
    return context, project, global_config


def test_windows_long_paths_change_only_the_selected_repository(
    repository_configuration: tuple[install.Context, Path, Path],
) -> None:
    context, project, global_config = repository_configuration
    before = global_config.read_bytes(), global_config.stat().st_mtime_ns
    step = next(item for item in install.STEPS if item.name == "Git long paths")
    assert install.run_steps([step], context, True, io.StringIO(), io.StringIO()) == 1
    assert install.run_steps([step], context, False, io.StringIO(), io.StringIO()) == 0
    local_config = project / ".git/config"
    assert "longpaths = true" in local_config.read_text(encoding="utf-8")
    converged = local_config.read_bytes(), local_config.stat().st_mtime_ns
    assert install.run_steps([step], context, False, io.StringIO(), io.StringIO()) == 0
    assert (local_config.read_bytes(), local_config.stat().st_mtime_ns) == converged
    assert (global_config.read_bytes(), global_config.stat().st_mtime_ns) == before


@pytest.mark.parametrize("windows", [False, True])
def test_long_paths_leave_unselected_repositories_and_global_settings_unchanged(
    repository_configuration: tuple[install.Context, Path, Path], windows: bool
) -> None:
    context, project, global_config = repository_configuration
    context.windows = windows
    (project / ".agents/plugins.json").unlink()
    local_config = project / ".git/config"
    before = local_config.read_bytes(), global_config.read_bytes()
    step = next(item for item in install.STEPS if item.name == "Git long paths")
    assert install.run_steps([step], context, False, io.StringIO(), io.StringIO()) == 0
    assert (local_config.read_bytes(), global_config.read_bytes()) == before
