#!/usr/bin/env python3
"""Generate project host settings from a repository's plugin selection (Python 3.11+)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import subprocess

import harness_permissions
import lock_io
import machine_paths
import plugin_selection

if sys.version_info < (3, 11):
    raise SystemExit("Repository settings generation requires Python 3.11 or newer")

host_permissions = plugin_selection.runtime_module("host_permissions")


MANAGED_START = "# BEGIN repo-declared agent capabilities"
MANAGED_END = "# END repo-declared agent capabilities"
TABLE = re.compile(r"^\s*\[\s*(marketplaces|plugins)(?:\.|\])")
HEADER = re.compile(r"^\s*\[")
OWNERS_FILE = "repo-owners.json"
GITHUB_OWNER = re.compile(r"github\.com[:/]+([^/]+)/", re.IGNORECASE)


def repo_owners() -> frozenset[str]:
    for parent in Path(__file__).resolve().parents:
        markers = (parent / ".git", parent / ".codex-plugin" / "plugin.json", parent / ".claude-plugin" / "plugin.json")
        candidate = parent / ".agents" / "agent" / "resources" / OWNERS_FILE
        if candidate.is_file() and any(marker.exists() for marker in markers):
            owners = lock_io.load_json(candidate).get("owners", [])
            return frozenset(owner.casefold() for owner in owners if isinstance(owner, str) and owner)
    return frozenset()


def remote_owners(root: Path, run=subprocess.run) -> set[str]:
    owners = set()
    for name in lock_io.git(run, ["remote"], root).split():
        for url in lock_io.git(run, ["remote", "get-url", "--all", name], root).splitlines():
            match = GITHUB_OWNER.search(url)
            owners.add(match.group(1).casefold() if match else url)
    return owners


def resolve_scope(root: Path, scope: str | None, run=subprocess.run) -> str:
    if scope not in {None, "project", "local"}:
        raise lock_io.LockError(f"Unknown repository scope: {scope}")
    if not (root / ".git").exists():
        return scope or "project"
    foreign = sorted(remote_owners(root, run) - repo_owners())
    if not foreign:
        return scope or "project"
    if scope == "project":
        raise lock_io.LockError(
            f"{root} belongs to {', '.join(foreign)}: agent settings there are local and git-excluded only; use --scope local"
        )
    return "local"


def declarations(lock_path: Path, lock: dict) -> tuple[dict[str, dict], dict[str, bool], list[str], list[dict]]:
    marketplaces, selected_identities = plugin_selection.validate(lock)
    sources = {name: {"owner_repository": source["repository"]} for name, source in marketplaces.items()}
    identities = {identity: True for identity in selected_identities}
    selected = {plugin_selection.overlay_id(identity) for identity in selected_identities}
    requirements = []
    overlay_path = lock_path.resolve().parent / "repository-harness.json"
    if overlay_path.is_file():
        overlay = lock_io.load_json(overlay_path)
        if not isinstance(overlay, dict) or set(overlay) != {"schema_version", "requires"} or overlay["schema_version"] != 1:
            raise lock_io.LockError(f"{overlay_path}: invalid repository harness overlay")
        harness_permissions.validate_requires(overlay["requires"], str(overlay_path))
        for required in overlay["requires"]["marketplaces"]:
            if not isinstance(required, dict) or set(required) != {"id", "repository"}:
                raise lock_io.LockError("Invalid required marketplace")
            source = sources.get(required["id"])
            if source is None or source["owner_repository"] != required["repository"]:
                raise lock_io.LockError(f"Required marketplace disagrees with the lock: {required['id']}")
        missing = sorted(set(overlay["requires"]["plugins"]) - selected)
        if missing:
            raise lock_io.LockError("Repository lock omits harness-required plugins: " + ", ".join(missing))
        requirements.append(overlay["requires"])
    claude_allow, codex_rules = harness_permissions.fold_permissions(requirements)
    claude_allow = [entry for entry in claude_allow if not harness_permissions.contains_template(entry)]
    codex_rules = [rule for rule in codex_rules if not harness_permissions.contains_template(rule)]
    return sources, identities, claude_allow, codex_rules


def claude_settings(path: Path, sources: dict[str, dict], identities: dict[str, bool], allow: list[str]) -> str:
    settings = lock_io.load_json(path) if path.is_file() else {}
    settings["extraKnownMarketplaces"] = {
        name: {"source": {"source": "github", "repo": release["owner_repository"]}, "autoUpdate": True}
        for name, release in sources.items()
    }
    settings["enabledPlugins"] = identities
    permissions = settings.get("permissions", {})
    if not isinstance(permissions, dict):
        raise lock_io.LockError(f"{path}: permissions must be an object")
    existing_allow = permissions.get("allow", [])
    if not isinstance(existing_allow, list) or any(not isinstance(entry, str) for entry in existing_allow):
        raise lock_io.LockError(f"{path}: permissions.allow must be a string list")
    unmanaged_allow = sorted(set(existing_allow) - set(allow))
    if unmanaged_allow:
        raise lock_io.LockError(f"{path}: unmanaged permissions.allow entries: " + ", ".join(unmanaged_allow))
    permissions["allow"] = allow
    settings["permissions"] = permissions
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def unmanaged_codex(text: str) -> str:
    if text.count(MANAGED_START) != text.count(MANAGED_END) or text.count(MANAGED_START) > 1:
        raise lock_io.LockError("Malformed managed section in .codex/config.toml")
    if MANAGED_START in text:
        before, rest = text.split(MANAGED_START, 1)
        _, after = rest.split(MANAGED_END, 1)
        text = before + after.lstrip("\r\n")
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    skip = False
    for line in lines:
        if HEADER.match(line):
            skip = bool(TABLE.match(line))
        if not skip:
            kept.append(line)
    return "".join(kept).rstrip()


def codex_settings(path: Path, sources: dict[str, dict], identities: dict[str, bool]) -> str:
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    prefix = unmanaged_codex(existing)
    return (prefix + "\n\n" if prefix else "") + codex_block(sources, identities)


def codex_block(sources: dict, identities: dict) -> str:
    lines = [MANAGED_START]
    for marketplace, release in sources.items():
        lines += [f"[marketplaces.{marketplace}]", 'source_type = "git"',
                  f'source = "https://github.com/{release["owner_repository"]}.git"']
        if marketplace == plugin_selection.MARKETPLACE_NAME:
            lines.append(f'sparse_paths = {json.dumps([".agents/plugins", ".claude-plugin", "plugins"])}')
        lines.append("")
    for identity, enabled in identities.items():
        lines += [f'[plugins."{identity}"]', f"enabled = {str(enabled).lower()}", ""]
    lines.append(MANAGED_END)
    return "\n".join(lines) + "\n"


def codex_rules(rules: list[dict]) -> str:
    lines = ["# Generated from repository-declared agent harness requirements.", ""]
    lines += harness_permissions.codex_rule_lines(rules)
    return "\n".join(lines)


LOCAL_STATE = ".agents/repo-capabilities.local.json"
EXCLUDE_START = "# BEGIN local repo-declared agent capabilities"
EXCLUDE_END = "# END local repo-declared agent capabilities"


def contained(root: Path, path: Path) -> Path:
    candidate = path if path.is_absolute() else root / path
    candidate = candidate.parent.resolve() / candidate.name
    if not candidate.is_relative_to(root) or not candidate.resolve().is_relative_to(root):
        raise lock_io.LockError(f"Target escapes repository root: {candidate}")
    for ancestor in candidate.parents:
        if ancestor == root:
            break
        if ancestor.exists() and not ancestor.is_dir():
            raise lock_io.LockError(f"Target parent is not a directory: {ancestor}")
    if candidate.exists() and not candidate.is_file():
        raise lock_io.LockError(f"Target is not a file: {candidate}")
    if candidate.is_file() and candidate.stat().st_nlink > 1:
        raise lock_io.LockError(f"Target has multiple hard links: {candidate}")
    return candidate


def file_identity(path: Path):
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def validate_aliases(targets: list[Path], read_paths: list[Path]) -> None:
    canonical = set()
    identities = set()
    inputs = {path.resolve() for path in read_paths}
    input_identities = {identity for path in read_paths if (identity := file_identity(path)) is not None}
    for path in targets:
        resolved = path.resolve()
        identity = file_identity(path)
        if resolved in canonical or identity is not None and identity in identities:
            raise lock_io.LockError("Repository configuration targets alias the same file")
        if resolved in inputs or identity is not None and identity in input_identities:
            raise lock_io.LockError(f"Repository configuration target aliases a read input: {path}")
        canonical.add(resolved)
        if identity is not None:
            identities.add(identity)


def preflight_paths(root: Path, paths: list[Path], *, scope: str = "local", read_paths: list[Path] = (), run=subprocess.run) -> Path | None:
    if scope not in {"project", "local"}:
        raise lock_io.LockError(f"Unknown repository scope: {scope}")
    root = root.expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise lock_io.LockError(f"Repository root is not a directory: {root}")
    if not root.exists() and not root.parent.is_dir():
        raise lock_io.LockError(f"Repository root parent does not exist: {root.parent}")
    lexical_targets = [Path(os.path.abspath(path if path.is_absolute() else root / path)) for path in paths]
    targets = [contained(root, path) for path in paths]
    if any(not path.is_relative_to(root) for path in lexical_targets):
        raise lock_io.LockError("Target escapes repository root")
    exclude = None
    if not root.exists() or not (root / ".git").exists():
        if any((parent / ".git").exists() for parent in root.parents):
            raise lock_io.LockError(f"Root is not the exact Git checkout: {root}")
    else:
        top = Path(lock_io.git(run, ["rev-parse", "--show-toplevel"], root)).resolve()
        if top != root:
            raise lock_io.LockError(f"Root is not the exact Git checkout: {root}")
        if scope == "local":
            tracked_paths = sorted({relative for lexical, target in zip(lexical_targets, targets) for relative in (lexical.relative_to(root).as_posix(), target.relative_to(root).as_posix(), target.resolve().relative_to(root).as_posix())})
            tracked = lock_io.git(run, ["--literal-pathspecs", "ls-files", "--cached", "-z", "--", *tracked_paths], root)
            if tracked:
                raise lock_io.LockError("Local targets are tracked: " + tracked.replace("\0", ", "))
            common = Path(lock_io.git(run, ["rev-parse", "--path-format=absolute", "--git-common-dir"], root)).resolve()
            exclude = Path(lock_io.git(run, ["rev-parse", "--git-path", "info/exclude"], root))
            if not exclude.is_absolute():
                exclude = root / exclude
            if not exclude.resolve().is_relative_to(common):
                raise lock_io.LockError(f"Git exclusion target escapes Git metadata: {exclude}")
            contained(common, exclude)
    validate_aliases([*targets, *([exclude] if exclude is not None else [])], read_paths)
    return exclude


def text_at(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def local_state(path: Path) -> dict:
    if not path.is_file():
        return {"schema_version": 1, "claude": {}, "codex": None, "rules": None}
    state = lock_io.load_json(path)
    if set(state) != {"schema_version", "claude", "codex", "rules"} or state["schema_version"] != 1:
        raise lock_io.LockError(f"{path}: invalid local ownership state")
    if not isinstance(state["claude"], dict) or any(state[key] is not None and not isinstance(state[key], str) for key in ("codex", "rules")):
        raise lock_io.LockError(f"{path}: invalid local ownership state")
    for key, value in state["claude"].items():
        if key not in {"extraKnownMarketplaces", "enabledPlugins", "allow"}:
            raise lock_io.LockError(f"{path}: invalid Claude ownership key")
        if key == "allow":
            lock_io.require_string_list(value, "owned Claude approvals")
        elif not isinstance(value, dict) or any(not isinstance(record, dict) or set(record) != {"present", "before", "written"} or not isinstance(record["present"], bool) for record in value.values()):
            raise lock_io.LockError(f"{path}: invalid Claude ownership records")
    return state


def local_claude(path: Path, sources: dict, identities: dict, allow: list[str], previous: dict) -> tuple[str, dict]:
    settings = lock_io.load_json(path) if path.is_file() else {}
    wanted = {
        "extraKnownMarketplaces": {name: {"source": {"source": "github", "repo": release["owner_repository"]}, "autoUpdate": True} for name, release in sources.items()},
        "enabledPlugins": identities,
    }
    owned = {}
    for key, desired in wanted.items():
        current = settings.get(key, {})
        if not isinstance(current, dict):
            raise lock_io.LockError(f"{path}: {key} must be an object")
        records = previous.get(key, {})
        for name, record in records.items():
            if name not in current or current[name] != record["written"]:
                raise lock_io.LockError(f"{path}: owned setting was changed: {key}.{name}")
            if record["present"]:
                current[name] = record["before"]
            else:
                current.pop(name, None)
        owned[key] = {}
        for name, value in desired.items():
            if name not in current or current[name] != value:
                owned[key][name] = {"present": name in current, "before": current.get(name), "written": value}
                current[name] = value
        settings[key] = current
    permissions = settings.get("permissions", {})
    if not isinstance(permissions, dict):
        raise lock_io.LockError(f"{path}: permissions must be an object")
    existing = permissions.get("allow", [])
    lock_io.require_string_list(existing, "Claude approvals")
    survivors = [entry for entry in existing if entry not in previous.get("allow", [])]
    owned["allow"] = [entry for entry in allow if entry not in survivors]
    permissions["allow"] = survivors + owned["allow"]
    settings["permissions"] = permissions
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n", owned


def local_codex(path: Path, sources: dict, identities: dict, previous: str | None) -> tuple[str, str]:
    try:
        import tomllib
    except ImportError as error:
        raise lock_io.LockError("Local repository settings require Python 3.11 or newer") from error
    existing = text_at(path) or ""
    if existing.count(MANAGED_START) != existing.count(MANAGED_END) or existing.count(MANAGED_START) > 1 or (MANAGED_START in existing and existing.index(MANAGED_START) > existing.index(MANAGED_END)):
        raise lock_io.LockError("Malformed managed section in .codex/config.toml")
    if MANAGED_START in existing:
        before, rest = existing.split(MANAGED_START, 1)
        body, after = rest.split(MANAGED_END, 1)
        block = MANAGED_START + body + MANAGED_END + "\n"
        if previous != block:
            raise lock_io.LockError("Codex managed section is not owned or was changed")
        prefix = before + after.lstrip("\r\n")
    else:
        prefix = existing
    try:
        parsed = tomllib.loads(prefix)
    except tomllib.TOMLDecodeError as error:
        raise lock_io.LockError(f"{path}: invalid unmanaged TOML: {error}") from error
    for key, desired in (("marketplaces", sources), ("plugins", identities)):
        foreign = parsed.get(key, {})
        if not isinstance(foreign, dict) or set(foreign) & set(desired):
            raise lock_io.LockError(f"{path}: unmanaged {key} collision")
    block = codex_block(sources, identities)
    expected = (prefix.rstrip() + "\n\n" if prefix.strip() else "") + block
    try:
        tomllib.loads(expected)
    except tomllib.TOMLDecodeError as error:
        raise lock_io.LockError(f"{path}: conflicting TOML tables: {error}") from error
    return expected, block


def exclusion_text(path: Path, root: Path, targets: list[Path]) -> str:
    existing = text_at(path) or ""
    owner = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
    relatives = {relative for target in targets for relative in (target.relative_to(root).as_posix(), target.resolve().relative_to(root).as_posix())}
    patterns = {"/" + re.sub(r"([*?\[\] ])", r"\\\1", relative) for relative in relatives}
    block = "\n".join([EXCLUDE_START + " " + owner, *sorted(patterns), EXCLUDE_END + " " + owner]) + "\n"
    kept = []
    active = None
    owners = set()
    for line in existing.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if stripped.startswith(EXCLUDE_START):
            suffix = stripped[len(EXCLUDE_START):]
            if active is not None or suffix in owners or suffix and not re.fullmatch(r" [a-f0-9]{64}", suffix):
                raise lock_io.LockError(f"{path}: malformed managed exclusions")
            active = suffix
            owners.add(suffix)
            if suffix == " " + owner:
                kept.append(block)
        elif stripped.startswith(EXCLUDE_END):
            if active is None or stripped != EXCLUDE_END + active:
                raise lock_io.LockError(f"{path}: malformed managed exclusions")
            if active != " " + owner:
                kept.append(line)
            active = None
            continue
        if active != " " + owner:
            kept.append(line)
    if active is not None:
        raise lock_io.LockError(f"{path}: malformed managed exclusions")
    existing = "".join(kept)
    if " " + owner in owners:
        return existing
    return existing + ("\n" if existing and not existing.endswith("\n") else "") + block


def plan(
    lock_path: Path,
    *,
    root: Path | None = None,
    scope: str | None = None,
    prospective_paths: list[Path] = (),
    lock_document: dict | None = None,
    read_paths: list[Path] = (),
    execute=subprocess.run,
) -> dict[Path, str | None]:
    root = (root or lock_path.resolve().parent.parent).expanduser().resolve()
    scope = resolve_scope(root, scope, execute)
    claude = root / ".claude" / ("settings.local.json" if scope == "local" else "settings.json")
    codex = root / ".codex/config.toml"
    rules_path = root / ".codex/rules/agent-harness.rules"
    state_path = root / LOCAL_STATE
    paths = [claude, codex, rules_path] + ([state_path] if scope == "local" else [])
    inputs = [*read_paths, lock_path.resolve().parent / "repository-harness.json", root / ".agents/settings.json"]
    if lock_document is None:
        inputs.append(lock_path)
    exclude = preflight_paths(root, [*paths, *prospective_paths], scope=scope, read_paths=inputs, run=execute)
    lock = lock_document if lock_document is not None else lock_io.load_json(lock_path)
    sources, identities, allow, rules = declarations(lock_path, lock)
    expected_rules = codex_rules(rules) if rules else None
    if scope == "local":
        state = local_state(state_path)
        claude_text, claude_owned = local_claude(claude, sources, identities, allow, state["claude"])
        codex_text, codex_owned = local_codex(codex, sources, identities, state["codex"])
        actual_rules = text_at(rules_path)
        if actual_rules is not None and actual_rules != state["rules"]:
            raise lock_io.LockError(f"{rules_path}: harness rules are not owned or were changed")
        updated = {"schema_version": 1, "claude": claude_owned, "codex": codex_owned, "rules": expected_rules}
        targets = {claude: claude_text, codex: codex_text, rules_path: expected_rules, state_path: json.dumps(updated, indent=2, ensure_ascii=False) + "\n"}
        if exclude is not None:
            selection = [lock_path] if lock_path.resolve().is_relative_to(root) else []
            targets[exclude] = exclusion_text(exclude, root, [*paths, *[contained(root, path) for path in [*prospective_paths, *selection]]])
    else:
        targets = {claude: claude_settings(claude, sources, identities, allow), codex: codex_settings(codex, sources, identities), rules_path: expected_rules}
    try:
        mode = host_permissions.permissions(root)
        targets[claude] = host_permissions.claude_settings(targets[claude], mode, project=True)
        targets[codex] = host_permissions.codex_settings(targets[codex], mode)
    except ValueError as error:
        raise lock_io.LockError(str(error)) from error
    if scope == "project":
        reject_machine_paths(root, targets)
    for path in targets:
        text_at(path)
    return targets


def reject_machine_paths(root: Path, targets: dict[Path, str | None]) -> None:
    committed = {**{root / relative: text_at(root / relative) for relative in machine_paths.COMMITTED_SETTINGS}, **targets}
    problems = [
        problem
        for path, text in committed.items()
        if text is not None
        for problem in machine_paths.describe(path.relative_to(root).as_posix(), text)
    ]
    if problems:
        raise lock_io.LockError("Committed host settings contain machine-specific paths:\n" + "\n".join(problems))


def write_batch(targets: dict[Path, str | None]) -> None:
    originals = {path: path.read_bytes() if path.is_file() else None for path in targets}
    attempted = []
    try:
        for path, expected in targets.items():
            attempted.append(path)
            if expected is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(expected, encoding="utf-8")
    except BaseException as error:
        failures = []
        for path in reversed(attempted):
            before = originals[path]
            try:
                actual = path.read_bytes() if path.is_file() else None
                if actual == before:
                    continue
                if before is None:
                    path.unlink()
                else:
                    path.write_bytes(before)
            except OSError as rollback_error:
                failures.append(f"{path}: {rollback_error}")
        if failures:
            raise lock_io.LockError("Repository configuration rollback failed: " + "; ".join(failures)) from error
        raise


def run(lock_path: Path, mode: str, *, root: Path | None = None, scope: str | None = None) -> list[str]:
    if mode not in {"write", "check", "preview"}:
        raise lock_io.LockError(f"Unknown repository mode: {mode}")
    root = (root or lock_path.resolve().parent.parent).expanduser().resolve()
    targets = plan(lock_path, root=root, scope=scope)
    changed = {path: expected for path, expected in targets.items() if text_at(path) != expected}
    drift = [path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path) for path in changed]
    if mode == "write":
        write_batch(changed)
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--mode", required=True, choices=("write", "check", "preview"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--scope", choices=("project", "local"))
    args = parser.parse_args(argv)
    try:
        lock = args.lock or plugin_selection.resolve((args.root or Path.cwd()).expanduser().resolve())
        drift = run(lock, args.mode, root=args.root, scope=args.scope)
    except (lock_io.LockError, OSError, UnicodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    if drift:
        print(("Updated" if args.mode == "write" else "Would update" if args.mode == "preview" else "Drift in") + ": " + ", ".join(drift))
    return int(args.mode == "check" and bool(drift))


if __name__ == "__main__":
    raise SystemExit(main())
