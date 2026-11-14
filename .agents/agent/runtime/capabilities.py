from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import assert_never, cast

from tj_agent.host import Host
from tj_cli import process


@dataclass(frozen=True, slots=True)
class Reference:
    repository: str
    path: PurePosixPath
    symbol: str

    @classmethod
    def parse(cls, value: str) -> Reference:
        repository, separator, target = value.partition(":")
        path, marker, symbol = target.partition("::")
        if (
            not separator
            or not marker
            or not re.fullmatch(r"[a-z][a-z-]*", repository)
            or not path
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or "\\" in path
            or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", symbol)
        ):
            raise ValueError(f"Invalid capability reference: {value!r}")
        return cls(repository, PurePosixPath(path), symbol)


@dataclass(frozen=True, slots=True)
class Support:
    implementation: str = ""
    contract: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.reason:
            if self.implementation or self.contract or len(self.reason.strip()) < 20:
                raise ValueError("A host exception requires only a concrete reason")
        elif not self.implementation or not self.contract:
            raise ValueError(
                "Every supported host needs an implementation and contract"
            )
        else:
            Reference.parse(self.implementation)
            contract = Reference.parse(self.contract)
            if not contract.symbol.rsplit(".", 1)[-1].startswith("test_") and (
                contract.path.suffix != ".rs"
            ):
                raise ValueError("A contract reference must name a test")

    def references(self) -> tuple[Reference, ...]:
        if self.reason:
            return ()
        return tuple(
            Reference.parse(value) for value in (self.implementation, self.contract)
        )


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    claude: Support
    codex: Support

    def support(self, host: Host) -> Support:
        match host:
            case Host.CLAUDE:
                return self.claude
            case Host.CODEX:
                return self.codex
            case _:
                assert_never(host)


REVISIONS = {
    "cli": "3a511d2717284678b91e337fedb0d50c103e617a",
    "personal": "7e0b2a962df9ba1b90284990360cdbd9c49cbe3d",
    "core": "03f99b363e11943cf8918a99b6a7654f5b770edb",
    "work-items": "0d107309e04bedd77d2b1b3834ad92db0a777d0c",
}

CAPABILITIES = (
    Capability(
        "session-command-environment",
        Support(
            "agent:.claude/runtime/launch.py::record_session_environment",
            "agent:tests/test_capability_contracts.py::test_session_environment_reaches_tool_commands",
        ),
        Support(
            "agent:.codex/runtime/launch.py::record_session_environment",
            "agent:tests/test_capability_contracts.py::test_session_environment_reaches_tool_commands",
        ),
    ),
    Capability(
        "native-executable-selection",
        Support(
            "agent:.claude/runtime/launch.py::executable",
            "agent:tests/test_capability_contracts.py::test_native_executable_selection",
        ),
        Support(
            "agent:.codex/runtime/launch.py::executable",
            "agent:tests/test_capability_contracts.py::test_native_executable_selection",
        ),
    ),
    Capability(
        "managed-launch-preparation",
        Support(
            "agent:.claude/runtime/launch.py::prepare",
            "agent:tests/test_capability_contracts.py::test_claude_preparation_preserves_native_command_without_creating_session",
        ),
        Support(
            "agent:.codex/runtime/launch.py::prepare",
            "agent:tests/test_codex_startup.py::test_launch_uses_exact_session_and_genuine_native_argv",
        ),
    ),
    Capability(
        "failed-launch-cleanup",
        Support(
            "agent:.claude/runtime/launch.py::abandon",
            "agent:tests/test_capability_contracts.py::test_claude_abandon_preserves_existing_session_without_external_calls",
        ),
        Support(
            "agent:.codex/runtime/launch.py::abandon",
            "agent:tests/test_codex_startup.py::test_launch_failure_cleans_only_before_dispatch_without_store_proof",
        ),
    ),
    Capability(
        "launch-prompt-model-effort",
        Support(
            "agent:.claude/runtime/launch.py::start_arguments",
            "agent:tests/test_capability_contracts.py::test_launch_preserves_prompt_model_and_effort",
        ),
        Support(
            "agent:.codex/runtime/launch.py::start_arguments",
            "agent:tests/test_capability_contracts.py::test_launch_preserves_prompt_model_and_effort",
        ),
    ),
    Capability(
        "handoff-parent-item",
        Support(
            "agent:.agents/agent/resources/handoff/launch_claude.py::main",
            "agent:tests/test_capability_contracts.py::test_handoff_preserves_item_parent_and_standing_rule",
        ),
        Support(
            "agent:.agents/agent/resources/handoff/launch_codex.py::main",
            "agent:tests/test_capability_contracts.py::test_handoff_preserves_item_parent_and_standing_rule",
        ),
    ),
    Capability(
        "session-registration",
        Support(
            "agent:.agents/agent/runtime/session_hook.py::start",
            "agent:tests/test_capability_contracts.py::test_session_start_binds_the_launch",
        ),
        Support(
            "agent:.agents/agent/runtime/session_hook.py::start",
            "agent:tests/test_capability_contracts.py::test_session_start_binds_the_launch",
        ),
    ),
    Capability(
        "session-naming",
        Support(
            "agent:.claude/runtime/launch.py::name_session",
            "agent:tests/test_capability_contracts.py::test_session_title_reaches_the_native_host",
        ),
        Support(
            "agent:.codex/runtime/launch.py::name_session",
            "agent:tests/test_capability_contracts.py::test_session_title_reaches_the_native_host",
        ),
    ),
    Capability(
        "host-liveness",
        Support(
            "agent:.claude/runtime/launch.py::liveness",
            "agent:tests/test_capability_contracts.py::test_native_liveness_reports_active_sessions",
        ),
        Support(
            "agent:.codex/runtime/launch.py::liveness",
            "agent:tests/test_capability_contracts.py::test_native_liveness_reports_active_sessions",
        ),
    ),
    Capability(
        "message-post",
        Support(
            "cli:src/message.rs::run",
            "cli:tests/message.rs::a_message_to_a_claude_session_reaches_its_native_inbox_from_either_host",
        ),
        Support(
            "cli:src/message.rs::run",
            "cli:tests/message.rs::a_message_to_a_codex_session_lands_in_its_feed_with_the_sender",
        ),
    ),
    Capability(
        "codex-feed-read",
        Support(
            reason="A Claude session gets tj message in its native inbox; no feed."
        ),
        Support(
            "cli:src/feed.rs::run",
            "cli:tests/message.rs::feed_wait_returns_when_a_message_arrives",
        ),
    ),
    Capability(
        "plugin-refresh",
        Support(
            "agent:.claude/runtime/installed.py::refresh",
            "agent:tests/test_capability_contracts.py::test_refresh_updates_selected_plugins",
        ),
        Support(
            "agent:.codex/runtime/installed.py::refresh",
            "agent:tests/test_capability_contracts.py::test_refresh_updates_selected_plugins",
        ),
    ),
    Capability(
        "wrapper-update",
        Support(
            "cli:src/run/update.rs::publish",
            "cli:tests/run.rs::each_claude_update_is_applied_to_an_idle_claude_in_place_once",
        ),
        Support(
            "cli:src/run/update.rs::publish",
            "cli:tests/run.rs::a_codex_update_is_applied_through_its_app_server",
        ),
    ),
    Capability(
        "hook-trust",
        Support(reason="Claude does not require per-hook hash trust."),
        Support(
            "agent:.codex/runtime/hook_trust.py::trust_installed",
            "agent:tests/test_hook_trust.py::"
            "test_installed_hooks_are_trusted_only_in_trusted_projects",
        ),
    ),
    Capability(
        "approval-session-scope",
        Support(
            "personal:.agents/personal/utility/approve/scripts/approve.py::current_session",
            "personal:.agents/tests/test_approve.py::ApproveTests.test_a_launch_id_resolves_the_launch_item_and_its_bound_session",
        ),
        Support(
            "personal:.agents/personal/utility/approve/scripts/approve.py::current_session",
            "personal:.agents/tests/test_approve.py::ApproveTests.test_a_codex_thread_resolves_its_native_session",
        ),
    ),
    Capability(
        "recovery",
        Support(
            "agent:.agents/agent/runtime/tj_recover.py::restore",
            "agent:tests/test_tj_recover.py::test_recover_previous_uptime_session_without_item",
        ),
        Support(
            "agent:.agents/agent/runtime/tj_recover.py::restore",
            "agent:tests/test_tj_recover.py::test_recover_previous_uptime_session_without_item",
        ),
    ),
    Capability(
        "session-resume",
        Support(
            "agent:.claude/runtime/launch.py::resume_arguments",
            "agent:tests/test_capability_contracts.py::test_resume_preserves_the_native_session",
        ),
        Support(
            "agent:.codex/runtime/launch.py::resume_arguments",
            "agent:tests/test_capability_contracts.py::test_resume_preserves_the_native_session",
        ),
    ),
    Capability(
        "background-respawn",
        Support(
            "agent:.claude/runtime/launch.py::respawn",
            "agent:tests/test_tj_recover.py::test_recover_background_respawns_in_place",
        ),
        Support(reason="Codex has no native background-session respawn."),
    ),
    Capability(
        "closeout",
        Support(
            "cli:src/run/closeout.rs::finish",
            "cli:tests/run.rs::closes_out_after_done_and_idle",
        ),
        Support(
            "cli:src/run/closeout.rs::finish",
            "cli:tests/run.rs::codex_is_stopped_with_a_ctrl_c_keypress",
        ),
    ),
    Capability(
        "plugin-enablement",
        Support(
            "agent:.claude/runtime/plugins.py::inventory_entries",
            "agent:tests/test_capability_contracts.py::test_plugin_inventory_reads_native_rows",
        ),
        Support(
            "agent:.codex/runtime/plugins.py::inventory_entries",
            "agent:tests/test_capability_contracts.py::test_plugin_inventory_reads_native_rows",
        ),
    ),
    Capability(
        "skill-availability",
        Support(
            "core:.agents/plugins/manifests/claude/workflow.json::skills",
            "core:tests/test_source_layout.py::SourceLayoutTests.test_each_host_adapter_resolves_one_canonical_definition",
        ),
        Support(
            "core:.agents/plugins/manifests/codex/workflow.json::skills",
            "core:tests/test_source_layout.py::SourceLayoutTests.test_each_host_adapter_resolves_one_canonical_definition",
        ),
    ),
    Capability(
        "permission-defaults",
        Support(
            "agent:.agents/agent/runtime/host_permissions.py::claude_settings",
            "agent:tests/test_capability_contracts.py::test_permissions_preserve_user_settings",
        ),
        Support(
            "agent:.agents/agent/runtime/host_permissions.py::codex_settings",
            "agent:tests/test_capability_contracts.py::test_permissions_preserve_user_settings",
        ),
    ),
    Capability(
        "install",
        Support(
            "agent:.agents/agent/runtime/install.py::apply_native_permissions",
            "agent:tests/test_install.py::NativePermissionsTests.test_native_permissions_follow_portable_mode_and_converge_for_both_platforms",
        ),
        Support(
            "agent:.agents/agent/runtime/install.py::apply_native_permissions",
            "agent:tests/test_install.py::NativePermissionsTests.test_native_permissions_follow_portable_mode_and_converge_for_both_platforms",
        ),
    ),
    Capability(
        "work-item-closeout-readiness",
        Support(
            "work-items:packages/tj-work-items/src/tj_work_items/cli.py::run",
            "work-items:.agents/tests/test_cli.py::test_prs_reports_all_merged_only_when_every_linked_pr_merged",
        ),
        Support(
            "work-items:packages/tj-work-items/src/tj_work_items/cli.py::run",
            "work-items:.agents/tests/test_cli.py::test_prs_reports_all_merged_only_when_every_linked_pr_merged",
        ),
    ),
)


def python_symbols(nodes: list[ast.stmt], prefix: str = "") -> set[str]:
    found = set()
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = prefix + node.name
            found.add(name)
            if isinstance(node, ast.ClassDef):
                found.update(python_symbols(node.body, name + "."))
    return found


def rust_code(text: str) -> str:
    tokens = re.compile(
        r"//[^\n]*|/\*|b?'(?:\\(?:u\{[\da-fA-F_]+\}|x[\da-fA-F]{2}|.)|[^'\\])'"
        r'|(?:br|cr|r)(?P<hashes>#{0,255})"[\s\S]*?"(?P=hashes)|[bc]?"(?:\\.|[^"\\])*"'
    )
    parts = []
    cursor = 0
    while token := tokens.search(text, cursor):
        parts.append(text[cursor : token.start()])
        parts.append(" ")
        cursor = token.end()
        if token[0] == "/*":
            depth = 1
            for boundary in re.finditer(r"/\*|\*/", text[cursor:]):
                depth += 1 if boundary[0] == "/*" else -1
                if depth == 0:
                    cursor += boundary.end()
                    break
            else:
                cursor = len(text)
    parts.append(text[cursor:])
    return "".join(parts)


def rust_tests(text: str) -> set[str]:
    functions = re.finditer(
        r"((?:\s*#\[[^\]]+\])*)\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)\s*\(",
        rust_code(text),
    )
    return {
        function[2]
        for function in functions
        if re.search(r"#\[\s*test\s*\]", function[1])
    }


@dataclass(frozen=True, slots=True)
class Source:
    root: Path
    revision: str | None = None

    def read(self, path: PurePosixPath) -> str:
        if self.revision is None:
            target = (self.root / path).resolve()
            if not target.is_relative_to(self.root.resolve()):
                raise ValueError(f"Reference escapes its repository: {path}")
            return target.read_text(encoding="utf-8")
        result = process.run(
            ["git", "-C", str(self.root), "show", f"{self.revision}:{path}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=60,
        )
        return cast(str, result.stdout)


def validate(
    sources: Mapping[str, Source],
    capabilities: tuple[Capability, ...] = CAPABILITIES,
    *,
    local_only: bool = False,
) -> list[str]:
    errors = []
    names = [capability.name for capability in capabilities]
    if (
        not names
        or any(not name.strip() for name in names)
        or len(set(names)) != len(names)
    ):
        errors.append("Capability names must be nonempty and unique")
    symbols: dict[tuple[str, PurePosixPath, bool], set[str]] = {}
    for capability in capabilities:
        for host in Host:
            for index, reference in enumerate(capability.support(host).references()):
                contract = index == 1
                label = (
                    f"{capability.name}/{host}: {reference.repository}:"
                    f"{reference.path}::{reference.symbol}"
                )
                if (
                    reference.repository != "agent"
                    and reference.repository not in REVISIONS
                ):
                    errors.append(f"{label}: repository has no immutable revision")
                    continue
                if local_only and reference.repository != "agent":
                    continue
                source = sources.get(reference.repository)
                if source is None:
                    errors.append(f"{label}: source was not supplied")
                    continue
                if (
                    reference.repository != "agent"
                    and source.revision != REVISIONS[reference.repository]
                ):
                    errors.append(f"{label}: source does not match its pinned revision")
                    continue
                key = reference.repository, reference.path, contract
                if key not in symbols:
                    try:
                        text = source.read(reference.path)
                        if reference.path.suffix == ".py":
                            symbols[key] = python_symbols(ast.parse(text).body)
                        elif reference.path.suffix == ".json":
                            symbols[key] = set(json.loads(text))
                        elif reference.path.suffix == ".rs":
                            symbols[key] = (
                                rust_tests(text)
                                if contract
                                else set(re.findall(r"\bfn\s+(\w+)\s*\(", text))
                            )
                        else:
                            raise ValueError("Unsupported capability source format")
                    except (
                        OSError,
                        ValueError,
                        SyntaxError,
                        subprocess.SubprocessError,
                    ) as error:
                        errors.append(f"{label}: {error}")
                        continue
                if reference.symbol not in symbols[key]:
                    errors.append(f"{label}: symbol does not exist")
    return errors


def fetch_sources(directory: Path) -> dict[str, Source]:
    sources = {}
    for repository, revision in REVISIONS.items():
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError(f"{repository} needs a full commit SHA")
        root = directory / repository
        commands = (
            ["git", "init", "--bare", str(root)],
            [
                "git",
                "-C",
                str(root),
                "fetch",
                "--depth=1",
                f"https://github.com/tj-agents/{repository}.git",
                revision,
            ],
        )
        for command in commands:
            process.run(command, check=True, capture_output=True, timeout=120)
        sources[repository] = Source(root, revision)
    return sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--local-only", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="host-capabilities-") as directory:
            sources = {} if arguments.local_only else fetch_sources(Path(directory))
            sources["agent"] = Source(arguments.root.resolve())
            errors = validate(sources, local_only=arguments.local_only)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Host capability check failed: {error}")
        return 1
    for failure in errors:
        print(failure)
    if errors:
        return 1
    scope = (
        "agent references only" if arguments.local_only else "all pinned repositories"
    )
    print(f"{len(CAPABILITIES)} capabilities checked for Claude and Codex ({scope})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
