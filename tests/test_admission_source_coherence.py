import hashlib
import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import managed_admission as admission
import plugin_refresh
import pytest
from tj_agent.plugins import sources

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "plugins/agent/.agents/agent/runtime"
ENTRYPOINTS = {
    "managed_admission.py": "host_plugins",
    "launch_check.py": "host",
    "startup_binding.py": "launch_check",
    "reload_plugins.py": "agent_cli",
}


@pytest.fixture
def snapshot() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "coherence_snapshot", ROOT / ".codex/hooks/snapshot.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def poisoned_module(path: Path, mode: py_compile.PycInvalidationMode) -> None:
    path.write_text('print("CACHED"); raise SystemExit(0)\n', encoding="utf-8")
    before = path.stat()
    py_compile.compile(str(path), doraise=True, invalidation_mode=mode)
    path.write_text('print("SOURCE"); raise SystemExit(0)\n', encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


@pytest.mark.parametrize("entrypoint,dependency", ENTRYPOINTS.items())
@pytest.mark.parametrize(
    "mode",
    [
        py_compile.PycInvalidationMode.TIMESTAMP,
        py_compile.PycInvalidationMode.UNCHECKED_HASH,
    ],
)
@pytest.mark.parametrize("use_runpy", [False, True])
def test_managed_entrypoints_ignore_existing_bytecode(
    tmp_path: Path,
    entrypoint: str,
    dependency: str,
    mode: py_compile.PycInvalidationMode,
    use_runpy: bool,
) -> None:
    script = tmp_path / entrypoint
    shutil.copyfile(RUNTIME / entrypoint, script)
    poisoned_module(tmp_path / (dependency + ".py"), mode)
    arguments = [str(script)]
    if use_runpy:
        arguments = [
            "-c",
            f"import runpy,sys; sys.path.insert(0, {str(tmp_path)!r}); "
            f"runpy.run_path({str(script)!r}, run_name='__main__')",
        ]
    result = subprocess.run(
        [sys.executable, "-B", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SOURCE"


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_library_import_does_not_change_interpreter_cache_state(
    entrypoint: str,
) -> None:
    script = RUNTIME / entrypoint
    program = (
        f"import runpy,sys; sys.path.insert(0, {str(RUNTIME)!r}); "
        "before=(sys.pycache_prefix,sys.dont_write_bytecode); "
        f"runpy.run_path({str(script)!r},run_name='library_probe'); "
        "assert (sys.pycache_prefix,sys.dont_write_bytecode)==before"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "mode",
    [
        py_compile.PycInvalidationMode.TIMESTAMP,
        py_compile.PycInvalidationMode.UNCHECKED_HASH,
    ],
)
@pytest.mark.parametrize("reuse", [False, True])
def test_codex_bootstrap_executes_source_in_cold_and_reused_snapshots(
    tmp_path: Path, snapshot: Any, mode: py_compile.PycInvalidationMode, reuse: bool
) -> None:
    package = tmp_path / "agent/version"
    package.mkdir(parents=True)
    loader = package / "hooks/codex/snapshot.py"
    loader.parent.mkdir(parents=True)
    loader.write_bytes((ROOT / ".codex/hooks/snapshot.py").read_bytes())
    target = package / "probe.py"
    target.write_text("import payload\n", encoding="utf-8")
    poisoned_module(package / "payload.py", mode)
    digest = snapshot.digest_tree(package)
    snapshots = tmp_path / "snapshots"
    selected = snapshot.Snapshot.for_package(package, digest, snapshots)
    if reuse:
        selected.resolve()
        poisoned_module(selected.path / "payload.py", mode)
    bootstrap = (ROOT / ".codex/hooks/bootstrap.py").read_text(encoding="utf-8")
    bootstrap = bootstrap.replace(
        "LOADER_DIGEST",
        repr(hashlib.sha256(loader.read_bytes().replace(b"\r\n", b"\n")).hexdigest()),
    )
    environment = {**os.environ, "TJ_AGENTS_STARTUP_SNAPSHOT_DIRECTORY": str(snapshots)}
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            bootstrap,
            str(package),
            digest,
            "agent",
            str(target),
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SOURCE"
    assert selected.path.is_dir()


@pytest.mark.parametrize(
    "mode",
    [
        py_compile.PycInvalidationMode.TIMESTAMP,
        py_compile.PycInvalidationMode.UNCHECKED_HASH,
    ],
)
def test_claude_dispatcher_executes_source(
    tmp_path: Path, mode: py_compile.PycInvalidationMode
) -> None:
    dispatcher = tmp_path / "hook_dispatch.py"
    shutil.copyfile(ROOT / ".agents/hooks/hook_dispatch.py", dispatcher)
    target = tmp_path / "probe.py"
    target.write_text("import payload\n", encoding="utf-8")
    poisoned_module(tmp_path / "payload.py", mode)
    result = subprocess.run(
        [sys.executable, "-B", str(dispatcher), "--hook", str(target)],
        input=json.dumps({"hook_event_name": "SessionStart"}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "CACHED" not in result.stdout + result.stderr
    assert (
        json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] == "SOURCE"
    )


@pytest.mark.parametrize(
    "relative",
    [
        "module.py",
        "module.pyc",
        "__pycache__/legacy.pyc",
        "__pycache__/unexpected.txt",
        "__pycache__/nested/module.pyc",
    ],
)
def test_snapshot_identity_retains_source_and_unexpected_cache_contents(
    tmp_path: Path, snapshot: Any, relative: str
) -> None:
    before = snapshot.digest_tree(tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"identity")
    assert snapshot.digest_tree(tmp_path) != before
    assert relative in plugin_refresh.package_tree(tmp_path)


def test_snapshot_copy_and_digest_ignore_only_generated_cache_bytes(
    tmp_path: Path, snapshot: Any
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "module.py").write_text("value = 1\n", encoding="utf-8")
    digest = snapshot.digest_tree(package)
    cache = package / "__pycache__/module.cpython-312.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"cache")
    assert snapshot.digest_tree(package) == digest
    selected = snapshot.Snapshot.for_package(package, digest, tmp_path / "snapshots")
    selected.resolve()
    assert not (selected.path / cache.relative_to(package)).exists()
    assert selected.reuse()


@pytest.mark.parametrize(
    "relative", ["__pycache__", "__pycache__/module.pyc", ".in_use/hidden"]
)
def test_cache_and_native_marker_exclusions_do_not_hide_links(
    tmp_path: Path, snapshot: Any, relative: str
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    link = package / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        if os.name != "nt":
            pytest.skip(f"symbolic links unavailable: {error}")
        subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=True,
        )
    with pytest.raises(admission.AdmissionError, match="link"):
        admission.signatures([package])
    with pytest.raises(plugin_refresh.RefreshError, match="symlink"):
        plugin_refresh.package_tree(package)
    with pytest.raises(SystemExit):
        snapshot.digest_tree(package)
    with pytest.raises(SystemExit):
        snapshot.generated_caches(str(link.parent), [link.name])


def test_publisher_checkout_materializes_original_bytes_with_replacement_refs(
    tmp_path: Path,
) -> None:
    publisher = tmp_path / "publisher"
    publisher.mkdir()

    def run(command: list[str], project: Path) -> str:
        return subprocess.run(
            command,
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

    def git(*arguments: str) -> str:
        return run(
            ["git", "--no-replace-objects", "-C", str(publisher), *arguments], tmp_path
        )

    git("init")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    value = publisher / "plugins/agent/value.txt"
    value.parent.mkdir(parents=True)
    value.write_text("original", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Original publication")
    original = git("rev-parse", "HEAD")
    value.write_text("replacement", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Replacement publication")
    replacement = git("rev-parse", "HEAD")

    def fetch_local(command: list[str], project: Path) -> str:
        if "fetch" not in command:
            return run(command, project)
        checkout = Path(command[command.index("-C") + 1])
        prefix = ["git", "-C", str(checkout)]
        run([*prefix, "fetch", str(publisher), original], project)
        run([*prefix, "fetch", str(publisher), replacement], project)
        run([*prefix, "replace", original, replacement], project)
        (checkout / ".git/FETCH_HEAD").write_text(original + "\n", encoding="ascii")
        return ""

    source = {
        "source": "git-subdir",
        "url": "https://github.com/tj-agents/agent.git",
        "sha": original,
        "path": "plugins/agent",
    }
    with patch.object(plugin_refresh, "run", side_effect=fetch_local):
        assert sources.tree(source, publisher, tmp_path, tmp_path / "staging", {}) == {
            "value.txt": b"original"
        }
