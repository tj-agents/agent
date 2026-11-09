"""Keep background execution behind the shared native process boundary."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    ".agents/agent/runtime",
    ".agents/agent/utility",
    ".codex/runtime",
    ".claude/runtime",
)
LAUNCHERS = {"run", "Popen", "call", "check_call", "check_output"}
FOREGROUND = {
    (".agents/agent/runtime/agent_cli.py", "open_tab"),
    (".agents/agent/runtime/agent_cli.py", "launch"),
}


class ProcessBoundaries(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.Module) -> None:
        self.path = path
        self.scope = "<module>"
        self.errors: list[str] = []
        self.modules = {"subprocess"}
        self.functions: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.modules.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "subprocess"
                )
            elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                self.functions.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name in LAUNCHERS
                )

    def raw(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in self.modules
            and node.attr in LAUNCHERS
        ) or (isinstance(node, ast.Name) and node.id in self.functions)

    def reject(self, node: ast.AST, reason: str) -> None:
        if (self.path, self.scope) not in FOREGROUND:
            self.errors.append(
                f"{self.path}:{getattr(node, 'lineno', 0)} {self.scope}: {reason}"
            )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        previous, self.scope = self.scope, node.name
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None and self.raw(default):
                self.reject(default, "background executor defaults to raw subprocess")
        self.generic_visit(node)
        self.scope = previous

    def visit_Call(self, node: ast.Call) -> None:
        if self.raw(node.func):
            self.reject(node, "use tj_cli.process")
        if any(keyword.arg == "creationflags" for keyword in node.keywords):
            self.reject(
                node, "window flags belong to the shared process implementation"
            )
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None and self.raw(node.value):
            self.reject(node, "executor defaults to raw subprocess")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self.raw(node.value):
            self.reject(node, "executor aliases raw subprocess")
        self.generic_visit(node)


def violations(source: str, path: str = "worker.py") -> list[str]:
    tree = ast.parse(source)
    visitor = ProcessBoundaries(path, tree)
    visitor.visit(tree)
    return visitor.errors


def test_background_launches_use_the_shared_process_boundary() -> None:
    errors = []
    for directory in SOURCE_ROOTS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            errors.extend(
                violations(
                    path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix()
                )
            )
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess as child\nchild.run(['git'])",
        "from subprocess import Popen as spawn\nspawn(['git'])",
        "import subprocess\nsubprocess.run(['git', '--', 'status'])",
        "import subprocess\ndef worker(run=subprocess.run): pass",
        "from tj_cli import process\nprocess.run(['git'], creationflags=1)",
    ],
)
def test_new_background_bypasses_are_rejected(source: str) -> None:
    assert violations(source)
