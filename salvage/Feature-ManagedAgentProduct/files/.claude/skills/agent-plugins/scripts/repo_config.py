#!/usr/bin/env python3
"""Generate project host settings from a repository's plugin selection (Python 3.11+)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import config_conflicts
import harness_permissions
import lock_io
import machine_paths
import plugin_selection
import published_harness
from plugin_runtime import Host, process
from tj_cli.git import repository_file

if sys.version_info < (3, 11):
    raise SystemExit("Repository settings generation requires Python 3.11 or newer")

host_permissions = plugin_selection.runtime_module("host_permissions")


MANAGED_START = "# BEGIN repo-declared agent capabilities"
MANAGED_END = "# END repo-declared agent capabilities"
TABLE = re.compile(r"^\s*\[\s*(marketplaces|plugins)(?:\.|\])")
HEADER = re.compile(r"^\s*\[")


def resolve_scope(root: Path, scope: str | None, run=process.run) -> str:
    if scope not in {None, "project", "local"}:
        raise lock_io.LockError(f"Unknown repository scope: {scope}")
    if scope is not None:
        return scope
    if not (root / ".git").exists():
        return "project"
    tracked = lock_io.git(
        run, ["--literal-pathspecs", "ls-files", "--cached", "-z", "--", ".agents/plugins.json"], root
    )
    return "project" if tracked else "local"


def declarations(
    lock_path: Path, lock: dict, root: Path, *, published: bool = True
) -> tuple[dict[str, dict], dict[str, bool], harness_permissions.Permissions | None]:
    """Return the selection's marketplaces, enabled plugins and committed permissions.

    Permissions compose every selected plugin's published declaration with the repository overlay
    (`harness_permissions.compose`). Without `published` they are None: the caller keeps its current ones.
    """
    marketplaces, selected_identities = plugin_selection.validate(lock)
    sources = {name: {"owner_repository": source["repository"]} for name, source in marketplaces.items()}
    identities = {identity: True for identity in selected_identities}
    selected = {plugin_selection.overlay_id(identity) for identity in selected_identities}
    overlay_permissions = harness_permissions.EMPTY_PERMISSIONS
    overlay_path = repository_file(root, ".agents/repository-harness.json")
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
        overlay_permissions = overlay["requires"].get("permissions", overlay_permissions)
    if not published:
        return sources, identities, None
    plugins = published_harness.declarations(marketplaces, selected_identities)
    return sources, identities, harness_permissions.compose(plugins.values(), overlay_permissions)


def claude_auto_update(existing: dict, name: str) -> dict[str, bool]:
    record = existing.get(name)
    value = record.get("autoUpdate") if isinstance(record, dict) else None
    if isinstance(value, bool):
        return {"autoUpdate": value}
    return {}


def claude_marketplaces(sources: dict[str, dict], existing: object) -> dict[str, dict]:
    existing = existing if isinstance(existing, dict) else {}
    return {
        name: {
            "source": {"source": "github", "repo": release["owner_repository"]},
            **claude_auto_update(existing, name),
        }
        for name, release in sources.items()
    }


def claude_lists(permissions: harness_permissions.Permissions) -> dict[str, list[str]]:
    return {"allow": list(permissions.claude_allow), "deny": list(permissions.claude_deny)}


def committed_permissions(root: Path) -> dict[str, set[str]]:
    """Return the Claude permission entries committed at HEAD, which regeneration may replace.

    An entry outside HEAD and the current declarations is a local edit that only exists in the working tree,
    so the writer refuses it rather than discarding it.
    """
    if not (root / ".git").exists():
        return {}
    text = machine_paths.tracked_snapshots(root, ".claude/settings.json").get("HEAD")
    try:
        settings = json.loads(text) if text is not None else {}
    except ValueError as error:
        raise lock_io.LockError(f"Committed .claude/settings.json is not valid JSON: {error}") from error
    permissions = settings.get("permissions") if isinstance(settings, dict) else None
    if not isinstance(permissions, dict):
        return {}
    return {
        key: {entry for entry in entries if isinstance(entry, str)}
        for key in ("allow", "deny")
        if isinstance(entries := permissions.get(key), list)
    }


def claude_settings(path: Path, sources: dict[str, dict], identities: dict[str, bool],
                    permissions: harness_permissions.Permissions | None,
                    committed: dict[str, set[str]] | None = None) -> str:
    settings = lock_io.load_json(path) if path.is_file() else {}
    settings["extraKnownMarketplaces"] = claude_marketplaces(sources, settings.get("extraKnownMarketplaces"))
    settings["enabledPlugins"] = identities
    if permissions is None:
        return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    current = settings.get("permissions", {})
    if not isinstance(current, dict):
        raise lock_io.LockError(f"{path}: permissions must be an object")
    for key, entries in claude_lists(permissions).items():
        existing = current.get(key, [])
        if not isinstance(existing, list) or any(not isinstance(entry, str) for entry in existing):
            raise lock_io.LockError(f"{path}: permissions.{key} must be a string list")
        unmanaged = sorted(set(existing) - set(entries) - (committed or {}).get(key, set()))
        if unmanaged:
            raise lock_io.LockError(f"{path}: unmanaged permissions.{key} entries: " + ", ".join(unmanaged))
        if entries or key == "allow":
            current[key] = entries
        else:
            current.pop(key, None)
    settings["permissions"] = current
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
    existing = config_conflicts.read_text(path, unmanaged=unmanaged_codex) if path.is_file() else ""
    if (
        existing == text_at(path)
        and existing.count(MANAGED_START) == 1
        and existing.count(MANAGED_END) == 1
        and existing.index(MANAGED_START) < existing.index(MANAGED_END)
    ):
        block = (
            existing[
                existing.index(MANAGED_START):
                existing.index(MANAGED_END) + len(MANAGED_END)
            ] + "\n"
        )
        return local_codex(path, sources, identities, block)[0]
    return codex_document(existing, sources, identities)


def codex_document(existing: str, sources: dict[str, dict], identities: dict[str, bool]) -> str:
    prefix = unmanaged_codex(existing)
    return (prefix + "\n\n" if prefix else "") + codex_block(sources, identities)


def codex_block(sources: dict, identities: dict) -> str:
    lines = [MANAGED_START]
    for marketplace, release in sources.items():
        lines += [f"[marketplaces.{marketplace}]", 'source_type = "git"',
                  f'source = "https://github.com/{release["owner_repository"]}.git"']
        if marketplace == plugin_selection.MARKETPLACE_NAME:
            sparse_paths = json.dumps(list(plugin_selection.MARKETPLACE_SPARSE_PATHS))
            lines.append(f"sparse_paths = {sparse_paths}")
        lines.append("")
    for identity, enabled in identities.items():
        lines += [f'[plugins."{identity}"]', f"enabled = {str(enabled).lower()}", ""]
    lines.append(MANAGED_END)
    return "\n".join(lines) + "\n"


def codex_rules(permissions: harness_permissions.Permissions) -> str | None:
    if not permissions.codex_allow and not permissions.codex_forbidden:
        return None
    lines = ["# Generated from the selected plugins' and this repository's harness permissions.", ""]
    lines += harness_permissions.codex_rule_lines(permissions.codex_allow, "allow")
    lines += harness_permissions.codex_rule_lines(permissions.codex_forbidden, "forbidden")
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


def preflight_paths(root: Path, paths: list[Path], *, scope: str = "local", read_paths: list[Path] = (), run=process.run) -> Path | None:
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
        top, git_common, git_exclude = lock_io.git(
            run, ["rev-parse", "--show-toplevel", "--path-format=absolute", "--git-common-dir", "--git-path", "info/exclude"], root
        ).splitlines()
        if Path(top).resolve() != root:
            raise lock_io.LockError(f"Root is not the exact Git checkout: {root}")
        if scope == "local":
            tracked_paths = sorted({relative for lexical, target in zip(lexical_targets, targets) for relative in (lexical.relative_to(root).as_posix(), target.relative_to(root).as_posix(), target.resolve().relative_to(root).as_posix())})
            tracked = lock_io.git(run, ["--literal-pathspecs", "ls-files", "--cached", "-z", "--", *tracked_paths], root)
            if tracked:
                raise lock_io.LockError("Local targets are tracked: " + tracked.replace("\0", ", "))
            common = Path(git_common).resolve()
            exclude = Path(git_exclude)
            if not exclude.resolve().is_relative_to(common):
                raise lock_io.LockError(f"Git exclusion target escapes Git metadata: {exclude}")
            contained(common, exclude)
    validate_aliases([*targets, *([exclude] if exclude is not None else [])], read_paths)
    return exclude


def text_at(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def local_state(path: Path) -> dict:
    if not path.is_file():
        return {"schema_version": 1, Host.CLAUDE.value: {}, Host.CODEX.value: None, "rules": None}
    state = lock_io.load_json(path)
    if set(state) != {"schema_version", Host.CLAUDE.value, Host.CODEX.value, "rules"} or state["schema_version"] != 1:
        raise lock_io.LockError(f"{path}: invalid local ownership state")
    if not isinstance(state[Host.CLAUDE.value], dict) or any(state[key] is not None and not isinstance(state[key], str) for key in (Host.CODEX.value, "rules")):
        raise lock_io.LockError(f"{path}: invalid local ownership state")
    for key, value in state[Host.CLAUDE.value].items():
        if key not in {"extraKnownMarketplaces", "enabledPlugins", "allow", "deny"}:
            raise lock_io.LockError(f"{path}: invalid Claude ownership key")
        if key in {"allow", "deny"}:
            lock_io.require_string_list(value, f"owned Claude permissions.{key}")
        elif not isinstance(value, dict) or any(not isinstance(record, dict) or set(record) != {"present", "before", "written"} or not isinstance(record["present"], bool) for record in value.values()):
            raise lock_io.LockError(f"{path}: invalid Claude ownership records")
    return state


def local_claude(path: Path, sources: dict, identities: dict, permissions: harness_permissions.Permissions | None,
                 previous: dict) -> tuple[str, dict]:
    settings = lock_io.load_json(path) if path.is_file() else {}
    wanted = {
        "extraKnownMarketplaces": claude_marketplaces(sources, settings.get("extraKnownMarketplaces")),
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
    if permissions is None:
        owned.update({key: previous[key] for key in ("allow", "deny") if key in previous})
        return json.dumps(settings, indent=2, ensure_ascii=False) + "\n", owned
    current = settings.get("permissions", {})
    if not isinstance(current, dict):
        raise lock_io.LockError(f"{path}: permissions must be an object")
    for key, entries in claude_lists(permissions).items():
        existing = current.get(key, [])
        lock_io.require_string_list(existing, f"Claude permissions.{key}")
        survivors = [entry for entry in existing if entry not in previous.get(key, [])]
        owned[key] = [entry for entry in entries if entry not in survivors]
        if survivors or owned[key] or key == "allow":
            current[key] = survivors + owned[key]
        else:
            current.pop(key, None)
    settings["permissions"] = current
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


def exclusion_text(
    path: Path, root: Path, targets: list[Path], *,
    existing: str | None = None, remove: bool = False,
) -> str:
    existing = (text_at(path) or "") if existing is None else existing
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
            if suffix == " " + owner and not remove:
                kept.append(block)
        elif stripped.startswith(EXCLUDE_END):
            if active is None or stripped != EXCLUDE_END + active:
                raise lock_io.LockError(f"{path}: malformed managed exclusions")
            if active != " " + owner:
                kept.append(line)
            active = None
            continue
        elif remove and active == " " + owner and stripped not in patterns:
            raise lock_io.LockError(f"{path}: owned exclusions were changed")
        if active != " " + owner:
            kept.append(line)
    if active is not None:
        raise lock_io.LockError(f"{path}: malformed managed exclusions")
    existing = "".join(kept)
    if " " + owner in owners or remove:
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
    execute=process.run,
    exclusions: dict[Path, str] | None = None,
    preserve_permissions: bool = False,
) -> dict[Path, str | None]:
    root = (root or lock_path.resolve().parent.parent).expanduser().resolve()
    scope = resolve_scope(root, scope, execute)
    claude = root / ".claude" / ("settings.local.json" if scope == "local" else "settings.json")
    codex = root / ".codex/config.toml"
    rules_path = root / ".codex/rules/agent-harness.rules"
    state_path = root / LOCAL_STATE
    paths = [claude, codex, rules_path] + ([state_path] if scope == "local" else [])
    inputs = [*read_paths, repository_file(root, ".agents/repository-harness.json"), root / ".agents/settings.json"]
    if lock_document is None:
        inputs.append(lock_path)
    exclude = preflight_paths(root, [*paths, *prospective_paths], scope=scope, read_paths=inputs, run=execute)
    lock = lock_document if lock_document is not None else lock_io.load_json(lock_path)
    sources, identities, permissions = declarations(lock_path, lock, root, published=not preserve_permissions)
    actual_rules = text_at(rules_path)
    expected_rules = actual_rules if permissions is None else codex_rules(permissions)
    if scope == "local":
        state = local_state(state_path)
        claude_text, claude_owned = local_claude(claude, sources, identities, permissions, state[Host.CLAUDE.value])
        codex_text, codex_owned = local_codex(codex, sources, identities, state[Host.CODEX.value])
        if actual_rules is not None and actual_rules != state["rules"]:
            raise lock_io.LockError(f"{rules_path}: harness rules are not owned or were changed")
        updated = {"schema_version": 1, Host.CLAUDE.value: claude_owned, Host.CODEX.value: codex_owned, "rules": expected_rules}
        targets = {claude: claude_text, codex: codex_text, rules_path: expected_rules, state_path: json.dumps(updated, indent=2, ensure_ascii=False) + "\n"}
        if exclude is not None:
            selection = [lock_path] if lock_path.resolve().is_relative_to(root) else []
            targets[exclude] = exclusion_text(exclude, root, [*paths, *[contained(root, path) for path in [*prospective_paths, *selection]]], existing=exclusions.get(exclude) if exclusions is not None else None)
            if exclusions is not None:
                exclusions[exclude] = targets[exclude]
    else:
        if state_path.exists():
            local_claude_path = root / ".claude/settings.local.json"
            exclude = preflight_paths(
                root, [local_claude_path, state_path], scope="local",
                read_paths=inputs, run=execute,
            )
            validate_aliases(
                [*paths, local_claude_path, state_path,
                 *([exclude] if exclude is not None else [])], inputs,
            )
            state = local_state(state_path)
            local_text, _ = local_claude(
                local_claude_path, {}, {}, harness_permissions.Permissions(),
                state[Host.CLAUDE.value],
            )
            current_codex = text_at(codex) or ""
            prior_codex = state[Host.CODEX.value]
            if prior_codex is not None and prior_codex not in current_codex:
                raise lock_io.LockError(
                    f"{codex}: owned managed section was changed or removed"
                )
            project_codex, _ = local_codex(codex, sources, identities, prior_codex)
            if actual_rules is not None and actual_rules != state["rules"]:
                raise lock_io.LockError(
                    f"{rules_path}: harness rules are not owned or were changed"
                )
        committed = committed_permissions(root) if permissions is not None else {}
        targets = {claude: claude_settings(claude, sources, identities, permissions, committed),
                   codex: codex_settings(codex, sources, identities), rules_path: expected_rules}
        if state_path.exists():
            targets[codex] = project_codex
            targets[local_claude_path] = local_text
            targets[state_path] = None
            if exclude is not None:
                local_paths = [
                    local_claude_path, codex, rules_path, state_path,
                    *prospective_paths,
                ]
                if lock_path.resolve().is_relative_to(root):
                    local_paths.append(lock_path)
                targets[exclude] = exclusion_text(
                    exclude, root, [contained(root, path) for path in local_paths],
                    existing=(
                        exclusions.get(exclude) if exclusions is not None else None
                    ),
                    remove=True,
                )
                if exclusions is not None:
                    exclusions[exclude] = targets[exclude]
    try:
        mode = host_permissions.permissions(root)
        targets[claude] = host_permissions.claude_settings(targets[claude], mode, project=True)
        targets[codex] = host_permissions.codex_settings(targets[codex], mode)
        if scope == "project":
            validation_targets = dict(targets)
            portable = machine_paths.portable_tracked_text(root, ".codex/config.toml")
            if portable is not None:
                validation_targets[codex] = host_permissions.codex_settings(
                    codex_document(portable, sources, identities), mode
                )
            reject_machine_paths(root, validation_targets)
    except ValueError as error:
        raise lock_io.LockError(str(error)) from error
    for path in targets:
        text_at(path)
    return targets


def reject_machine_paths(root: Path, targets: dict[Path, str | None]) -> None:
    committed = {
        **{root / relative: text_at(root / relative)
           for relative in machine_paths.COMMITTED_SETTINGS},
        **{path: text for path, text in targets.items() if path.is_relative_to(root)},
    }
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


def run(lock_path: Path, mode: str, *, root: Path | None = None, scope: str | None = None,
        preserve_permissions: bool = False) -> list[str]:
    if mode not in {"write", "check", "preview"}:
        raise lock_io.LockError(f"Unknown repository mode: {mode}")
    root = (root or lock_path.resolve().parent.parent).expanduser().resolve()
    targets = plan(lock_path, root=root, scope=scope, preserve_permissions=preserve_permissions)
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
    parser.add_argument("--preserve-permissions", action="store_true",
                        help="keep the current permissions instead of resolving the selected plugins' published ones")
    args = parser.parse_args(argv)
    try:
        root = (args.root or Path.cwd()).expanduser().resolve()
        lock = args.lock or plugin_selection.resolve(root)
        drift = run(lock, args.mode, root=root, scope=args.scope, preserve_permissions=args.preserve_permissions)
    except (lock_io.LockError, OSError, UnicodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    if drift:
        print(("Updated" if args.mode == "write" else "Would update" if args.mode == "preview" else "Drift in") + ": " + ", ".join(drift))
    return int(args.mode == "check" and bool(drift))


if __name__ == "__main__":
    raise SystemExit(main())
