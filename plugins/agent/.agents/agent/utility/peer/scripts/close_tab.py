"""List and close exact terminal tabs without focus keystrokes (Python 3.9+)."""

import argparse
import fnmatch
import json
import os
import sys
from pathlib import Path

import register_session
import session_close

register_session.native_hosts()
from tj_cli import processes
from tj_cli.close import terminal


class TerminalUnavailable(processes.Refusal):
    """The platform adapter cannot identify a terminal target."""


def is_windows():
    return os.name == "nt"


def nonempty_text(value):
    if not isinstance(value, str):
        return None
    return value if value.strip() else None


def entry_terminal_identity(entry):
    identity = entry.get("terminal")
    if not isinstance(identity, dict):
        return None
    if identity.get("kind") != "windows-terminal":
        return identity
    binding = identity.get("tab_binding")
    runtime_id = nonempty_text(binding.get("runtime_id")) if isinstance(binding, dict) else None
    if not runtime_id:
        runtime_id = nonempty_text(identity.get("runtime_id"))
    if runtime_id:
        return {**identity, "runtime_id": runtime_id}
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
    identity = {"kind": "windows-terminal"} if is_windows() else register_session.terminal_identity()
    return actual_targets(identity) if identity and identity.get("kind") in {"kitty", "tmux", "windows-terminal"} else []


def actual_targets(identity):
    return [{"title": item["title"], "identity": dict(item), "actual": True}
            for item in terminal.inventory(identity)]


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
        left_id = nonempty_text(left.get("runtime_id"))
        right_id = nonempty_text(right.get("runtime_id"))
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
                    terminal.native_roots(target["identity"])
                    target["query_status"] = "verified"
                except processes.Refusal as error:
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


def close_actual(target, force=False, fallback_entry=None):
    def guard():
        if target_liveness(target, fallback_entry) is not False and not force:
            raise processes.Refusal("target is live or unknown; pass --force only to close an explicit tab")
    terminal.close_identity(target["identity"], before_close=guard)


def close_stale_entry(entry):
    """Close a dead peer's exact, re-enumerated tab without signalling any process."""
    identity = entry.get("terminal")
    title = entry.get("title")
    if not isinstance(identity, dict) or not title:
        raise processes.Refusal("a stale tab needs a recorded terminal identity and title")
    kind = identity.get("kind")
    if kind not in {"kitty", "tmux", "windows-terminal"}:
        raise processes.Refusal("stale-tab close is unsupported for this terminal")
    targets = actual_targets(identity)
    title_only = kind == "windows-terminal" and not nonempty_text(identity.get("runtime_id"))
    if title_only:
        registered = [item for _path, item in session_close.entries()
                      if item.get("title") == title]
        if len(registered) != 1:
            raise processes.Refusal("the recorded stale terminal title is absent or ambiguous")
    matches = [target for target in targets if target["title"] == title
               and (title_only or same_target(target["identity"], identity))]
    if len(matches) != 1:
        raise processes.Refusal("the recorded stale terminal tab is absent or ambiguous")
    fallback_entry = registered[0] if title_only else None
    target_live = target_liveness(matches[0], fallback_entry)
    if target_live is not False:
        raise processes.Refusal("the re-enumerated stale terminal target is live or unknown")
    close_actual(matches[0], fallback_entry=fallback_entry)


def close_entry(entry, force=False):
    identity = entry_terminal_identity(entry)
    if identity is None:
        raise processes.Refusal("no native terminal identity was recorded")
    if identity.get("kind") == "konsole":
        raise TerminalUnavailable("Konsole cannot verify exact tab closure")
    def guard():
        if session_close.liveness(entry) is not True:
            raise processes.Refusal("registered host identity is not verified live")
        terminal.verify_process_binding(identity, {"pid": entry["pid"], "started_at": entry["pid_started_at"]})
    terminal.close_identity(identity, before_close=guard)


def focus_entry(entry):
    identity = entry_terminal_identity(entry)
    if identity is None:
        raise processes.Refusal("no native terminal identity was recorded")
    if session_close.liveness(entry) is not True:
        raise processes.Refusal("registered host identity is not verified live")
    terminal.verify_process_binding(identity, {"pid": entry["pid"], "started_at": entry["pid_started_at"]})
    terminal.focus(identity)


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
            raise processes.Refusal("the requested session is absent or ambiguous")
        if args.focus:
            focus_entry(found[0])
        else:
            close_entry(found[0], force=args.force)
        return 0
    if not title:
        parser.error("--title (or a positional title), --session, or --list is required")
    matches, wildcard = resolve_title(merged_inventory(), title)
    if not matches:
        raise processes.Refusal("no actual terminal tab has that title; run --list to inspect targets")
    if wildcard and not args.all:
        raise processes.Refusal("wildcard titles only report matches; pass --all to close them")
    if not wildcard and len(matches) != 1:
        raise processes.Refusal("exact title is ambiguous; use a unique title")
    for target in matches:
        close_actual(target, force=args.force)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except processes.Refusal as error:
        print("close-tab: " + str(error), file=sys.stderr)
        raise SystemExit(2)
