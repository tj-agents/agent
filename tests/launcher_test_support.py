"""Shared test fixtures for Agent's private Claude and Codex handoff launchers.

Loads the real agent_cli.py with only the functions that would otherwise touch a real executable or
terminal replaced, so resolve_tab_directory, prompt_file_argument, open_claude_tab and resolve_lane_model
all run for real in a launcher's own test suite -- the directory check, lane resolution and
standards-sync-before-launch ordering are exercised as written, never re-implemented in a stub.

Deliberately not named test_*.py, so neither unittest discovery nor pytest collects it as a test module.
"""
import importlib.util
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
AGENT_CLI = ROOT / '.agents/agent/runtime/agent_cli.py'

# A plausible resolve_codex_executable() result: a (path, version) pair shaped like the real function's
# return value, for a launch_codex.py test that never runs a real codex binary.
CODEX_EXECUTABLE = ('/bin/codex', {'core': (0, 160, 0), 'prerelease': '', 'text': '0.160.0'})


def _load_agent_cli_module(name):
    spec = importlib.util.spec_from_file_location(name, AGENT_CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def real_agent_cli(name, claude='/bin/claude'):
    """The real shared library with resolve_claude_executable, sync_claude_standards and launch_tab
    replaced -- the three functions that would otherwise touch a real executable or terminal. `name` is
    the module registry name, distinct per caller so two tests loading this in the same process never
    collide in `sys.modules`.
    """
    module = _load_agent_cli_module(name)
    module.resolve_claude_executable = mock.Mock(return_value=claude)
    module.sync_claude_standards = mock.Mock()
    module.launch_tab = mock.Mock()
    module.await_handoff_startup = mock.Mock()
    return module


def real_agent_cli_for_e2e(name, run, claude='/bin/claude'):
    """Load the real agent_cli.py fresh with its `launch_tab` default `run=subprocess.run` parameter bound
    to `run`.

    That default is fixed once, when the module's `def launch_tab(...)` line first executes, so the patch
    must be in place while the module loads -- patching `subprocess.run` afterward would not reach a
    default already bound to the original function object. `resolve_claude_executable` and
    `sync_claude_standards` are replaced after loading instead, because open_claude_tab resolves both by a
    plain name lookup in this module's globals at call time, not through a default parameter.
    """
    with mock.patch('subprocess.run', side_effect=run):
        module = _load_agent_cli_module(name)
    module.resolve_claude_executable = mock.Mock(return_value=claude)
    module.sync_claude_standards = mock.Mock()
    module.await_handoff_startup = mock.Mock()
    return module


def real_agent_cli_for_codex(name, codex=CODEX_EXECUTABLE):
    """The real shared library with resolve_codex_executable and launch_tab replaced -- the two functions
    that would otherwise touch a real executable or terminal. `name` is the module registry name, distinct
    per caller so two tests loading this in the same process never collide in `sys.modules`.
    """
    module = _load_agent_cli_module(name)
    module.resolve_codex_executable = mock.Mock(return_value=codex)
    module.launch_tab = mock.Mock()
    module.await_handoff_startup = mock.Mock()
    return module


def real_agent_cli_for_codex_e2e(name, run, codex=CODEX_EXECUTABLE):
    """Load the real agent_cli.py fresh with its `launch_tab` default `run=subprocess.run` parameter bound
    to `run`, same reasoning as real_agent_cli_for_e2e above."""
    with mock.patch('subprocess.run', side_effect=run):
        module = _load_agent_cli_module(name)
    module.resolve_codex_executable = mock.Mock(return_value=codex)
    module.await_handoff_startup = mock.Mock()
    return module
