r"""Reconcile the Claude plugin cache against the installed-plugin registry.

The registry is the truth. A cached version directory survives only when a live `installPath` names
it. Install and update already converge; removal never did, so every plugin rename left its
predecessor on disk permanently and nothing reconciled the two.

Unlike `skill_router.manifest_install_roots`, an unreadable registry is fatal here rather than a
fallback to walking the cache: "no live paths" must never be read as "everything is prunable".

The registry describes the next session, not the running one. A session that started before an update
still has hooks registered against the directory it resolved at startup, so `--pin` records that set and
every later pass treats a live session's pins as live too. Removal is therefore a liveness check, not a
side effect of update.

Codex is deliberately out of scope; see https://github.com/tj-agents/core/issues/383 for the condition that changes that.
"""

import argparse
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

CACHE_DEPTH = 3
NOTICE_INTERVAL_SECONDS = 60 * 60
STATE_DIRECTORY_ENV = "AGENT_STATE_DIRECTORY"
PIN_DIRECTORY = "plugin-pins"
# A pin younger than this is honoured whatever its pid says. The hook's parent may be a shell that has
# already exited, which would otherwise read as a dead session the moment it was recorded.
PIN_GRACE_SECONDS = 12 * 60 * 60
HOST_ORPHAN_MARKER = ".orphaned_at"
HOST_ORPHAN_GRACE_SECONDS = 14 * 24 * 60 * 60

LIVE = "live"
PINNED = "pinned"
GRACE = "grace"
RETAINED = "retained"
STALE = "stale"
ORPHAN = "orphan"

EXIT_OK = 0
EXIT_REGISTRY = 2
EXIT_REMOVAL = 3


class RegistryUnusable(Exception):
    """The registry could not establish which install paths are live."""


def claude_config_root(environ=None, home=None):
    values = os.environ if environ is None else environ
    configured = values.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured)
    return (Path.home() if home is None else home) / ".claude"


def registry_path(config_root):
    return config_root / "plugins" / "installed_plugins.json"


def cache_root(config_root):
    return config_root / "plugins" / "cache"


def normalize(path):
    """Comparison key for an install path.

    The registry spells Windows paths with escaped backslashes and inconsistent case, so the same
    directory arrives spelled differently from JSON than from a directory walk.
    """
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        resolved = Path(path)
    return os.path.normcase(str(resolved).rstrip("\\/"))


def live_install_paths(config_root):
    """Every install path the registry names, as normalized keys.

    Raises RegistryUnusable rather than returning an empty set, which would classify the entire
    cache as prunable.
    """
    path = registry_path(config_root)
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise RegistryUnusable(f"cannot read {path}: {error}") from error
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise RegistryUnusable(f"{path} is not valid JSON: {error}") from error
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        raise RegistryUnusable(f"{path} has no plugins object")
    paths = set()
    for installs in plugins.values():
        if not isinstance(installs, list):
            continue
        for install in installs:
            if not isinstance(install, dict):
                continue
            value = install.get("installPath")
            if isinstance(value, str) and value.strip():
                paths.add(normalize(value))
    if not paths:
        raise RegistryUnusable(f"{path} names no install paths")
    return paths


def is_reparse_point(path):
    """A junction or symlink. Never recursed into - a delete would follow it out of the cache."""
    if path.is_symlink():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def cached_versions(root):
    """Every `<marketplace>/<plugin>/<version>` directory, sorted."""
    if not root.is_dir():
        return []
    found = []
    try:
        marketplaces = sorted(entry for entry in root.iterdir() if entry.is_dir())
    except OSError:
        return []
    for marketplace in marketplaces:
        if is_reparse_point(marketplace):
            continue
        try:
            plugins = sorted(entry for entry in marketplace.iterdir() if entry.is_dir())
        except OSError:
            continue
        for plugin in plugins:
            if is_reparse_point(plugin):
                continue
            try:
                found.extend(sorted(entry for entry in plugin.iterdir() if entry.is_dir()))
            except OSError:
                continue
    return found


def modified_at(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def in_host_grace(path, now):
    marker = path / HOST_ORPHAN_MARKER
    try:
        orphaned_at = int(marker.read_text(encoding="utf-8").strip()) / 1000
    except (OSError, ValueError):
        try:
            orphaned_at = marker.stat().st_mtime
        except OSError:
            return False
    return 0 <= now - orphaned_at < HOST_ORPHAN_GRACE_SECONDS


def classify(versions, live, pinned=(), keep_previous=0, now=None):
    """Label every cached version directory. Returns [(path, state)] in scan order.

    A plugin with no live version is orphaned outright - the rename case, where nothing is coming
    back. Retention applies only to plugins that are still installed.

    A pin outranks every dead state and is not counted against retention: it is not a version being
    kept back, it is a directory a running session is using. The host's own orphan window is treated
    the same way.
    """
    moment = time.time() if now is None else now
    by_plugin = {}
    for version in versions:
        by_plugin.setdefault(version.parent, []).append(version)

    held = set(pinned)
    states = {}
    for entries in by_plugin.values():
        alive = [entry for entry in entries if normalize(entry) in live]
        for entry in alive:
            states[entry] = LIVE
        for entry in entries:
            if entry not in states and normalize(entry) in held:
                states[entry] = PINNED
            elif entry not in states and in_host_grace(entry, moment):
                states[entry] = GRACE
        dead = [entry for entry in entries if entry not in states]
        if not alive:
            states.update((entry, ORPHAN) for entry in dead)
            continue
        # Directory names are commit shas, so "previous" can only come from mtime.
        dead.sort(key=modified_at, reverse=True)
        for index, entry in enumerate(dead):
            states[entry] = RETAINED if index < keep_previous else STALE
    return [(version, states[version]) for version in versions]


def directory_size(path):
    total = 0
    for current, directories, files in os.walk(path):
        directories[:] = [
            name for name in directories if not is_reparse_point(Path(current) / name)
        ]
        for name in files:
            try:
                total += (Path(current) / name).lstat().st_size
            except OSError:
                continue
    return total


def human_bytes(count):
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _force_writable(function, path, _excinfo):
    """Windows refuses to unlink a read-only file, and a cached git checkout is full of them."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove_directory(path):
    if is_reparse_point(path):
        path.unlink()
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_writable)
    else:
        shutil.rmtree(path, onerror=_force_writable)


def assert_within(root, target):
    """A removal target must be exactly `<cache>/<marketplace>/<plugin>/<version>`."""
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{target} is outside {root}") from error
    if len(relative.parts) != CACHE_DEPTH:
        raise ValueError(f"{target} is not a cached version directory")


def missing_install_paths(live, versions):
    return sorted(live - {normalize(version) for version in versions})


def state_directory(environ=None, home=None):
    values = os.environ if environ is None else environ
    configured = values.get(STATE_DIRECTORY_ENV)
    if configured:
        return Path(configured)
    return (Path.home() if home is None else home) / ".agents-state"


def pin_directory(environ=None, home=None):
    return state_directory(environ, home) / PIN_DIRECTORY


def process_is_alive(pid):
    """Whether `pid` still names a running process. Undecidable resolves to alive.

    Every ambiguous answer keeps a directory. Keeping a prunable directory costs disk and converges on
    the next pass; deleting one a session is bound to is the failure this whole pin exists to stop.
    """
    if not isinstance(pid, int) or pid <= 0:
        return True
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        ERROR_INVALID_PARAMETER = 87
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return kernel32.GetLastError() != ERROR_INVALID_PARAMETER
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def pin_is_live(entry, now, grace=PIN_GRACE_SECONDS):
    started = entry.get("started_at")
    if isinstance(started, (int, float)) and 0 <= now - started < grace:
        return True
    return process_is_alive(entry.get("pid"))


def read_pins(environ=None, home=None, now=None, grace=PIN_GRACE_SECONDS):
    """Install paths held by still-running sessions, and the pin files that no longer hold anything.

    An unreadable or malformed pin is expired rather than trusted: it names no paths to protect, so
    keeping it would only defer its own removal forever.
    """
    moment = time.time() if now is None else now
    root = pin_directory(environ, home)
    held, expired = set(), []
    try:
        files = sorted(entry for entry in root.glob("*.json") if entry.is_file())
    except OSError:
        return held, expired
    for file in files:
        try:
            entry = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            expired.append(file)
            continue
        if not isinstance(entry, dict) or not pin_is_live(entry, moment, grace):
            expired.append(file)
            continue
        for value in entry.get("paths") or []:
            if isinstance(value, str) and value.strip():
                held.add(normalize(value))
    return held, expired


def sweep_pins(expired):
    for file in expired:
        try:
            file.unlink()
        except OSError:
            continue


def held_by(path):
    """The paths an existing pin file holds, or an empty set when it holds nothing readable."""
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    values = entry.get("paths") if isinstance(entry, dict) else None
    return {value for value in values or [] if isinstance(value, str) and value.strip()}


def record_pin(config_root, pid, now, environ=None, home=None):
    """This session's pin. Returns the entry, or None when there is nothing trustworthy to record.

    A pin file is keyed by pid, and two sessions can share one parent, so the paths are unioned with
    whatever the file already holds rather than replacing them. Replacing would drop the older
    session's directory while it was still bound to it - the one way this tool could still cause the
    failure it exists to prevent. Over-retention is bounded by expiry; losing a pin is not bounded.
    """
    try:
        paths = set(live_install_paths(config_root))
    except RegistryUnusable:
        return None
    destination = pin_directory(environ, home) / f"{pid}.json"
    entry = {"pid": pid, "started_at": now, "paths": sorted(paths | held_by(destination))}
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    staging.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(staging, destination)
    return entry


def notice_is_due(path, now, interval=NOTICE_INTERVAL_SECONDS):
    try:
        last = float(json.loads(path.read_text(encoding="utf-8")).get("last_notice", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return True
    return not 0 <= now - last < interval


def record_notice(path, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"last_notice": now}) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def reconcile(config_root, keep_previous=0, environ=None, home=None, now=None):
    live = live_install_paths(config_root)
    held, expired = read_pins(environ, home, now)
    root = cache_root(config_root)
    versions = cached_versions(root)
    return {
        "cache_root": root,
        "live": live,
        "pinned": held - live,
        "expired_pins": expired,
        "entries": classify(versions, live, held, keep_previous, now),
        "missing": missing_install_paths(live, versions),
    }


def prunable(entries):
    return [path for path, state in entries if state in (STALE, ORPHAN)]


def render_report(result, apply_mode, removed, failures, stream):
    entries = result["entries"]
    targets = prunable(entries)
    counts = {}
    for _, state in entries:
        counts[state] = counts.get(state, 0) + 1

    print(f"cache root: {result['cache_root']}", file=stream)
    print(
        f"registry: {len(result['live'])} live install paths, "
        f"{len(entries)} cached version directories",
        file=stream,
    )
    for state in (LIVE, PINNED, GRACE, RETAINED, STALE, ORPHAN):
        if counts.get(state):
            print(f"  {state:<9} {counts[state]}", file=stream)
    if counts.get(PINNED):
        print(
            f"  {counts[PINNED]} directory(s) held by a running session and not removed",
            file=stream,
        )
    if counts.get(GRACE):
        print(
            f"  {counts[GRACE]} directory(s) inside Claude's 14-day orphan window and not removed",
            file=stream,
        )

    for path in result["missing"]:
        print(f"warning: registry names a missing install path: {path}", file=stream)

    if not targets:
        print("nothing to prune", file=stream)
        return

    total = 0
    for path, state in entries:
        if state not in (STALE, ORPHAN):
            continue
        if path in failures:
            print(f"  FAILED    {path}  {failures[path]}", file=stream)
            continue
        size = removed[path] if path in removed else directory_size(path)
        total += size
        print(f"  {state:<9} {path}  ({human_bytes(size)})", file=stream)
    verb = "removed" if apply_mode else "would remove"
    print(
        f"{verb} {len(targets) - len(failures)} directories, {human_bytes(total)}",
        file=stream,
    )
    if failures:
        print(f"{len(failures)} removals failed", file=stream)
    elif not apply_mode:
        print(f"re-run with --apply to remove them: {Path(__file__).resolve()}", file=stream)


def run_pin(config_root, now, environ=None, home=None):
    """SessionStart bookkeeping: hold what this session resolved, drop what no session holds.

    Contract: exit 0 whatever happens. A pin that fails must never stop a session starting, and a
    missing pin degrades to registry-only liveness - which is where this tool started.
    """
    try:
        _, expired = read_pins(environ, home, now)
        sweep_pins(expired)
        record_pin(config_root, os.getppid(), now, environ, home)
    except Exception:  # noqa: BLE001 - bookkeeping must never stop a session starting
        return EXIT_OK
    return EXIT_OK


def run_notice(config_root, keep_previous, now, environ=None, home=None, stream=sys.stdout):
    """One line when the cache has drifted, silence otherwise. Never removes anything."""
    marker = state_directory(environ, home) / "plugin-cache-notice.json"
    if not notice_is_due(marker, now):
        return EXIT_OK
    try:
        result = reconcile(config_root, keep_previous, environ, home, now)
    except RegistryUnusable:
        return EXIT_OK
    targets = prunable(result["entries"])
    if targets:
        print(
            f"plugin cache: {len(targets)} version directories the registry no longer names. "
            f"Review with: python -B \"{Path(__file__).resolve()}\"",
            file=stream,
        )
    try:
        record_notice(marker, now)
    except OSError:
        pass
    return EXIT_OK


def run_reconcile(config_root, keep_previous, apply_mode, stream=sys.stdout, environ=None, home=None):
    try:
        result = reconcile(config_root, keep_previous, environ, home)
    except RegistryUnusable as error:
        print(f"error: {error}", file=sys.stderr)
        print("refusing to prune: the registry must establish what is live", file=sys.stderr)
        return EXIT_REGISTRY

    removed = {}
    failures = {}
    if apply_mode:
        # Only here: a dry run reads the pin registry but leaves it exactly as it found it.
        sweep_pins(result["expired_pins"])
        for path in prunable(result["entries"]):
            try:
                assert_within(result["cache_root"], path)
                size = directory_size(path)
                remove_directory(path)
            except (OSError, ValueError) as error:
                failures[path] = error
                continue
            removed[path] = size

    render_report(result, apply_mode, removed, failures, stream)
    return EXIT_REMOVAL if failures else EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="prune_plugin_cache",
        description="Reconcile the Claude plugin cache against the installed-plugin registry.",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="Claude configuration directory. Defaults to CLAUDE_CONFIG_DIR or ~/.claude.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Remove the reported directories. Without it the run only reports.",
    )
    parser.add_argument(
        "--keep-previous",
        type=int,
        default=0,
        metavar="N",
        help="Keep N non-live versions of a still-installed plugin, newest first by mtime.",
    )
    parser.add_argument(
        "--notice",
        action="store_true",
        help="Throttled cache-drift line. Never removes.",
    )
    parser.add_argument(
        "--pin",
        action="store_true",
        help="Record this session's live install paths so no later pass removes what it is bound to.",
    )
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    if arguments.keep_previous < 0:
        print("error: --keep-previous must not be negative", file=sys.stderr)
        return EXIT_REGISTRY
    config_root = arguments.config_dir or claude_config_root()
    now = time.time()
    if arguments.pin:
        run_pin(config_root, now)
        if not arguments.notice:
            return EXIT_OK
    if arguments.notice:
        return run_notice(config_root, arguments.keep_previous, now)
    return run_reconcile(config_root, arguments.keep_previous, arguments.apply)


if __name__ == "__main__":
    sys.exit(main())
