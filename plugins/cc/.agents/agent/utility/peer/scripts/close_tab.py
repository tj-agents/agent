"""List and close exact terminal tabs without focus keystrokes (Python 3.9+)."""

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import register_session
import session_close


class TerminalRefusal(RuntimeError):
    pass


class TerminalUnavailable(TerminalRefusal):
    """The platform adapter cannot identify a terminal target."""


CLOSE_CONFIRM_ATTEMPTS = 5
CLOSE_CONFIRM_INTERVAL_SECONDS = 0.1


def is_windows():
    return os.name == "nt"


def nonempty_text(value):
    if not isinstance(value, str):
        return None
    return value if value.strip() else None


def run(command):
    try:
        return subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", check=False,
            timeout=session_close.SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TerminalRefusal("terminal control command failed: " + str(error)) from error


def host_belongs_to(entry, roots):
    if not session_close.verified_live(entry.get("pid"), entry.get("pid_started_at")):
        return False
    target = entry["pid"]
    child_started = entry["pid_started_at"]
    seen = set()
    while target not in seen:
        seen.add(target)
        info = session_close.process_info(target)
        if (info is None or not session_close.valid_identity(info.pid, info.started_at)
                or info.started_at > child_started + session_close.IDENTITY_TOLERANCE_SECONDS):
            return False
        if target in roots:
            return True
        child_started = info.started_at
        target = info.ppid
    return False


def exact_process(value):
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return session_close.process_info(pid)


def kitty_inventory(identity):
    result = run(["kitty", "@", "--to", identity["listen_on"], "ls"])
    if result.returncode:
        raise TerminalRefusal((result.stderr or result.stdout or "kitty inventory failed").strip())
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise TerminalRefusal("kitty returned malformed inventory") from error
    if not isinstance(value, list):
        raise TerminalRefusal("kitty returned malformed inventory")
    targets = []
    for os_window in value:
        if not isinstance(os_window, dict):
            continue
        for tab in os_window.get("tabs", []):
            if not isinstance(tab, dict):
                continue
            for window in tab.get("windows", []):
                if not isinstance(window, dict) or window.get("id") is None:
                    continue
                targets.append({
                    "title": str(window.get("title") or ""),
                    "identity": {"kind": "kitty", "listen_on": identity["listen_on"],
                                 "window_id": str(window["id"])},
                    "actual": True,
                    "window": window,
                    "tab": tab,
                })
    return targets


def kitty_target(identity):
    matches = [target for target in kitty_inventory(identity)
               if target["identity"]["window_id"] == str(identity.get("window_id"))]
    if len(matches) != 1:
        raise TerminalRefusal("the exact kitty window is absent or ambiguous")
    return matches[0]["tab"], matches[0]["window"]


def tmux_command(identity, *arguments):
    command = ["tmux"]
    if identity.get("socket"):
        command.extend(["-S", identity["socket"]])
    command.extend(arguments)
    return command


def tmux_inventory(identity):
    result = run(tmux_command(identity, "list-panes", "-a", "-F",
                              "#{pane_id}\t#{pane_pid}\t#{pane_title}"))
    if result.returncode:
        raise TerminalRefusal((result.stderr or result.stdout or "tmux inventory failed").strip())
    targets = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or not parts[0]:
            continue
        pane, pid, title = parts
        identity_value = {"kind": "tmux", "pane_id": pane}
        if identity.get("socket"):
            identity_value["socket"] = identity["socket"]
        targets.append({"title": title, "identity": identity_value,
                        "actual": True, "pane_pid": pid})
    return targets


def tmux_target(identity):
    matches = [target for target in tmux_inventory(identity)
               if target["identity"]["pane_id"] == identity.get("pane_id")]
    if len(matches) != 1:
        raise TerminalRefusal("the exact tmux pane is absent or ambiguous")
    root = exact_process(matches[0]["pane_pid"])
    if root is None:
        raise TerminalRefusal("the exact tmux pane no longer has a queryable root process")
    return matches[0]["identity"]["pane_id"], root


def windows_inventory():
    if not is_windows():
        return None
    adapter = Path(__file__).with_name("windows_terminal_uia.ps1")
    if not adapter.is_file():
        raise TerminalUnavailable("Windows Terminal adapter is unavailable")
    result = run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                  str(adapter), "-Json"])
    if result.returncode:
        raise TerminalUnavailable(
            (result.stderr or result.stdout or "Windows Terminal inventory failed").strip())
    try:
        value = json.loads(result.stdout or "[]")
    except ValueError as error:
        raise TerminalUnavailable("Windows Terminal returned malformed inventory") from error
    if not isinstance(value, list):
        raise TerminalUnavailable("Windows Terminal returned a non-array inventory")
    targets = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        automation_id = nonempty_text(item.get("automationId"))
        if not automation_id and not title:
            continue
        identity = {"kind": "windows-terminal"}
        if automation_id:
            identity["automation_id"] = automation_id
        else:
            identity["title"] = title
        targets.append({
            "title": title,
            "identity": identity,
            "actual": True,
        })
    return targets


def entry_terminal_identity(entry):
    identity = entry.get("terminal")
    if not isinstance(identity, dict):
        return None
    if identity.get("kind") != "windows-terminal":
        return identity
    automation_id = nonempty_text(identity.get("automation_id"))
    if automation_id:
        return {**identity, "automation_id": automation_id}
    title = entry.get("title")
    if not isinstance(title, str) or not title:
        return dict(identity)
    return {**identity, "title": title}


def registered_targets():
    targets = []
    for _path, entry in session_close.entries():
        identity = entry_terminal_identity(entry)
        if identity is None:
            continue
        targets.append({"title": entry.get("title") or "", "identity": identity,
                        "actual": False, "entry": entry,
                        "live": session_close.liveness(entry)})
    return targets


def current_terminal_targets():
    if is_windows():
        return windows_inventory() or []
    identity = register_session.terminal_identity()
    if identity is None:
        return []
    kind = identity.get("kind")
    if kind == "kitty":
        return kitty_inventory(identity)
    if kind == "tmux":
        return tmux_inventory(identity)
    return []


def inventory_for_identity(identity):
    kind = identity.get("kind")
    if kind == "kitty":
        return kitty_inventory(identity)
    if kind == "tmux":
        return tmux_inventory(identity)
    if kind == "windows-terminal":
        return windows_inventory()
    raise TerminalUnavailable("the terminal cannot verify an exact tab closure")


def require_closed(identity):
    fallback_title = None
    if identity.get("kind") == "windows-terminal" and not nonempty_text(
            identity.get("automation_id")):
        fallback_title = nonempty_text(identity.get("title"))
    for attempt in range(CLOSE_CONFIRM_ATTEMPTS):
        targets = inventory_for_identity(identity)
        if fallback_title:
            remains = any(target["title"] == fallback_title for target in targets)
        else:
            remains = any(same_target(target["identity"], identity) for target in targets)
        if not remains:
            return
        if attempt + 1 < CLOSE_CONFIRM_ATTEMPTS:
            time.sleep(CLOSE_CONFIRM_INTERVAL_SECONDS)
    raise TerminalRefusal("the terminal did not confirm the exact tab closed")


def same_target(left, right):
    if left.get("kind") != right.get("kind"):
        return False
    kind = left.get("kind")
    if kind == "kitty":
        return (str(left.get("window_id")) == str(right.get("window_id"))
                and left.get("listen_on") == right.get("listen_on"))
    if kind == "tmux":
        return (left.get("pane_id") == right.get("pane_id")
                and left.get("socket") == right.get("socket"))
    if kind == "windows-terminal":
        left_id = nonempty_text(left.get("automation_id"))
        right_id = nonempty_text(right.get("automation_id"))
        if left_id or right_id:
            return bool(left_id and right_id and left_id == right_id)
        left_title = nonempty_text(left.get("title"))
        right_title = nonempty_text(right.get("title"))
        return bool(left_title and right_title and left_title == right_title)
    if kind == "konsole":
        return (left.get("service"), left.get("session")) == (
            right.get("service"), right.get("session"))
    return False


def target_liveness(target, fallback_entry=None):
    matched = []
    targets = registered_targets()
    for registered in targets:
        entry = registered["entry"]
        if same_target(entry_terminal_identity(entry) or {}, target.get("identity", {})):
            matched.append(registered["live"])
    if not matched and fallback_entry is not None:
        titled = [item for item in targets if item["entry"].get("title") == target.get("title")]
        if (len(titled) == 1
                and session_close.same_session_process(titled[0]["entry"], fallback_entry)):
            matched.append(titled[0]["live"])
    if not matched:
        return None
    if any(value is True for value in matched):
        return True
    if any(value is None for value in matched):
        return None
    return False


def merged_inventory():
    actual = current_terminal_targets()
    for target in actual:
        target["live"] = target_liveness(target)
    for target in registered_targets():
        if not any(target["title"] == actual_target["title"]
                   and same_target(target["identity"], actual_target["identity"])
                   for actual_target in actual):
            target["listing_limit"] = (
                "Konsole is listed from the registry; global Konsole tab inventory is unsupported."
                if target["identity"].get("kind") == "konsole" else None)
            if target["identity"].get("kind") == "konsole":
                try:
                    konsole_roots(target["identity"])
                    target["query_status"] = "verified"
                except TerminalRefusal as error:
                    target["query_status"] = str(error)
            actual.append(target)
    return actual


def has_wildcard(title):
    return any(character in title for character in "*?[")


def resolve_title(targets, title):
    if has_wildcard(title):
        return ([target for target in targets if target["actual"]
                 and fnmatch.fnmatchcase(target["title"], title)], True)
    return ([target for target in targets if target["actual"]
             and target["title"] == title], False)


def qdbus_path(identity):
    executable = shutil.which("qdbus6") or shutil.which("qdbus") or shutil.which("qdbus-qt5")
    service = identity.get("service")
    session = identity.get("session")
    if not executable or not service or not isinstance(session, str) or not session.startswith("/"):
        raise TerminalRefusal(
            "Konsole needs qdbus6/qdbus and its recorded service and full session path")
    return executable, service, session


def konsole_roots(identity):
    executable, service, session = qdbus_path(identity)
    roots = []
    for method in ("org.kde.konsole.Session.processId",
                   "org.kde.konsole.Session.foregroundProcessId"):
        queried = run([executable, service, session, method])
        if queried.returncode:
            raise TerminalRefusal((queried.stderr or queried.stdout or "Konsole query failed").strip())
        info = exact_process(queried.stdout.strip())
        if info is not None:
            roots.append(info.pid)
    if not roots:
        raise TerminalRefusal("the recorded Konsole session has no queryable process")
    return roots


def close_actual(target, force=False, fallback_entry=None):
    live = target_liveness(target, fallback_entry)
    if live is not False and not force:
        raise TerminalRefusal("target is live or unknown; pass --force only to close an explicit tab")
    identity = target["identity"]
    kind = identity["kind"]
    if kind == "kitty":
        result = run(["kitty", "@", "--to", identity["listen_on"], "close-window",
                      "--match", "id:" + str(identity["window_id"])])
    elif kind == "tmux":
        result = run(tmux_command(identity, "kill-pane", "-t", identity["pane_id"]))
    elif kind == "windows-terminal":
        adapter = Path(__file__).with_name("windows_terminal_uia.ps1")
        if not is_windows() or not adapter.is_file():
            raise TerminalUnavailable("Windows Terminal control is available only on Windows")
        automation_id = nonempty_text(identity.get("automation_id"))
        if automation_id:
            selector = ["-TabId", automation_id]
        else:
            title = nonempty_text(identity.get("title"))
            if not title:
                raise TerminalRefusal("Windows Terminal target has no automation ID or title")
            selector = ["-Title", title]
        result = run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                      str(adapter), *selector])
    else:
        raise TerminalRefusal("global inventory cannot safely close this terminal target")
    if result.returncode:
        raise TerminalRefusal((result.stderr or result.stdout or "terminal refused close").strip())
    require_closed(identity)


def close_stale_entry(entry):
    """Close a dead peer's exact, re-enumerated tab without signalling any process."""
    identity = entry.get("terminal")
    title = entry.get("title")
    if not isinstance(identity, dict) or not title:
        raise TerminalRefusal("a stale tab needs a recorded terminal identity and title")
    kind = identity.get("kind")
    if kind == "kitty":
        targets = kitty_inventory(identity)
    elif kind == "tmux":
        targets = tmux_inventory(identity)
    elif kind == "windows-terminal":
        targets = windows_inventory()
    else:
        raise TerminalRefusal("stale-tab close is unsupported for this terminal")
    title_only = kind == "windows-terminal" and not nonempty_text(identity.get("automation_id"))
    if title_only:
        registered = [item for _path, item in session_close.entries()
                      if item.get("title") == title]
        if len(registered) != 1:
            raise TerminalRefusal("the recorded stale terminal title is absent or ambiguous")
    matches = [target for target in targets if target["title"] == title
               and (title_only or same_target(target["identity"], identity))]
    if len(matches) != 1:
        raise TerminalRefusal("the recorded stale terminal tab is absent or ambiguous")
    fallback_entry = registered[0] if title_only else None
    target_live = target_liveness(matches[0], fallback_entry)
    if target_live is not False:
        raise TerminalRefusal("the re-enumerated stale terminal target is live or unknown")
    close_actual(matches[0], fallback_entry=fallback_entry)


def close_entry(entry, force=False):
    identity = entry.get("terminal")
    if not isinstance(identity, dict):
        raise TerminalRefusal("no native terminal identity was recorded")
    live = session_close.liveness(entry)
    if live is None:
        raise TerminalRefusal("registered host identity is unknown")
    if live is not True:
        raise TerminalRefusal(
            "a stale registered host cannot prove terminal ownership; use close_tab.py --title")
    kind = identity.get("kind")
    if kind == "kitty":
        window, listen = identity.get("window_id"), identity.get("listen_on")
        if not window or not listen:
            raise TerminalRefusal("kitty identity is incomplete")
        if live is not True:
            raise TerminalRefusal("a stale kitty window cannot be safely attributed after its host exited")
        _tab, target = kitty_target(identity)
        roots = [target.get("pid")]
        roots.extend(item.get("pid") for item in target.get("foreground_processes", [])
                     if isinstance(item, dict))
        root_pids = [info.pid for item in roots if (info := exact_process(item))]
        if not host_belongs_to(entry, root_pids):
            raise TerminalRefusal("registered host is not owned by the exact kitty window")
        result = run(["kitty", "@", "--to", listen, "close-window", "--match", "id:" + str(window)])
    elif kind == "tmux":
        if live is not True:
            raise TerminalRefusal("a stale tmux pane cannot be safely attributed after its host exited")
        pane, root = tmux_target(identity)
        if not host_belongs_to(entry, [root.pid]):
            raise TerminalRefusal("registered host is not owned by the exact tmux pane")
        result = run(tmux_command(identity, "kill-pane", "-t", pane))
    elif kind == "konsole":
        if live is not True:
            raise TerminalRefusal("a stale Konsole session cannot be safely attributed after its host exited")
        roots = konsole_roots(identity)
        if not host_belongs_to(entry, roots):
            raise TerminalRefusal("registered host is not owned by the exact Konsole session")
        raise TerminalUnavailable("Konsole cannot verify exact tab closure")
    elif kind == "windows-terminal":
        if not is_windows():
            raise TerminalUnavailable("Windows Terminal control is available only on Windows")
        targets = windows_inventory()
        title = entry.get("title")
        tab_id = nonempty_text(identity.get("automation_id"))
        if tab_id:
            matches = [target for target in targets
                       if target["identity"].get("automation_id") == tab_id]
        else:
            registered = [item for _path, item in session_close.entries()
                          if item.get("title") == title]
            if not title or len(registered) != 1:
                raise TerminalRefusal("Windows Terminal fallback requires a unique registered tab title")
            matches = [target for target in targets if target["title"] == title]
        if len(matches) != 1:
            raise TerminalRefusal("Windows Terminal target is absent or ambiguous")
        close_actual(matches[0], force=True)
        return
    else:
        raise TerminalRefusal("unsupported terminal kind")
    if result.returncode:
        raise TerminalRefusal((result.stderr or result.stdout or "terminal refused close").strip())
    require_closed(identity)


def focus_entry(entry):
    """Bring a verified-live registered session's exact tab to the front without sending keystrokes."""
    identity = entry_terminal_identity(entry)
    if identity is None:
        raise TerminalRefusal("no native terminal identity was recorded")
    if session_close.liveness(entry) is not True:
        raise TerminalRefusal("registered host identity is not verified live")
    kind = identity.get("kind")
    if kind == "kitty":
        kitty_target(identity)
        result = run(["kitty", "@", "--to", identity["listen_on"], "focus-window",
                      "--match", "id:" + str(identity["window_id"])])
    elif kind == "tmux":
        pane, _root = tmux_target(identity)
        result = run(tmux_command(identity, "select-window", "-t", pane))
        if not result.returncode:
            result = run(tmux_command(identity, "select-pane", "-t", pane))
        if not result.returncode:
            run(tmux_command(identity, "switch-client", "-t", pane))
    elif kind == "windows-terminal":
        adapter = Path(__file__).with_name("windows_terminal_uia.ps1")
        if not is_windows() or not adapter.is_file():
            raise TerminalUnavailable("Windows Terminal control is available only on Windows")
        automation_id = nonempty_text(identity.get("automation_id"))
        title = nonempty_text(identity.get("title"))
        if automation_id:
            matches = [target for target in windows_inventory()
                       if target["identity"].get("automation_id") == automation_id]
            selector = ["-TabId", automation_id]
        elif title:
            registered = [item for _path, item in session_close.entries() if item.get("title") == title]
            if len(registered) != 1:
                raise TerminalRefusal("Windows Terminal fallback requires a unique registered tab title")
            matches = [target for target in windows_inventory() if target["title"] == title]
            selector = ["-Title", title]
        else:
            raise TerminalRefusal("Windows Terminal target has no automation ID or title")
        if len(matches) != 1:
            raise TerminalRefusal("Windows Terminal target is absent or ambiguous")
        result = run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                      str(adapter), *selector, "-Focus"])
    else:
        raise TerminalUnavailable("this terminal cannot focus an exact tab")
    if result.returncode:
        raise TerminalRefusal((result.stderr or result.stdout or "terminal refused focus").strip())


def print_inventory(targets):
    payload = [{
        "actual": target["actual"],
        "kind": target["identity"].get("kind"),
        "live": target.get("live"),
        "listing_limit": target.get("listing_limit"),
        "query_status": target.get("query_status"),
        "title": target["title"],
    } for target in targets]
    print(json.dumps(payload, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title", nargs="?")
    parser.add_argument("--title", dest="named_title")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--session")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--focus", action="store_true", help="focus the --session tab instead of closing it")
    args = parser.parse_args(argv)
    if args.focus and not args.session:
        parser.error("--focus requires --session")
    if args.title and args.named_title:
        parser.error("specify a title once")
    title = args.named_title or args.title
    if args.list:
        if title or args.session:
            parser.error("--list does not accept a title or --session")
        print_inventory(merged_inventory())
        return 0
    if args.session:
        if title:
            parser.error("--session cannot be combined with a title")
        found = [item for _path, item in session_close.entries()
                 if item.get("session_id") == args.session]
        if len(found) != 1:
            raise TerminalRefusal("the requested session is absent or ambiguous")
        if args.focus:
            focus_entry(found[0])
        else:
            close_entry(found[0], force=args.force)
        return 0
    if not title:
        parser.error("--title (or a positional title), --session, or --list is required")
    matches, wildcard = resolve_title(merged_inventory(), title)
    if not matches:
        raise TerminalRefusal("no actual terminal tab has that title; run --list to inspect targets")
    if wildcard and not args.all:
        raise TerminalRefusal("wildcard titles only report matches; pass --all to close them")
    if not wildcard and len(matches) != 1:
        raise TerminalRefusal("exact title is ambiguous; use a unique title")
    for target in matches:
        close_actual(target, force=args.force)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TerminalRefusal as error:
        print("close-tab: " + str(error), file=sys.stderr)
        raise SystemExit(2)
