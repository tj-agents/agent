"""Configure Windows Terminal profile defaults to close exited tabs (Python 3.9+)."""

import argparse
import datetime
import os
import re
import shutil
from pathlib import Path


CLOSE_VALUES = ("always", "graceful", "automatic", "never")
DEFAULTS_OPEN = re.compile(r'"defaults"\s*:\s*\{')
CLOSE_VALUE = re.compile(r'("closeOnExit"\s*:\s*")([^"]+)(")')


def is_windows():
    return os.name == "nt"


def default_settings_path(environ=None):
    values = os.environ if environ is None else environ
    local_app_data = values.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is not set; pass --settings-path explicitly")
    return (Path(local_app_data) / "Packages"
            / "Microsoft.WindowsTerminal_8wekyb3d8bbwe" / "LocalState"
            / "settings.json")


def update_close_on_exit(text, value):
    existing = CLOSE_VALUE.search(text)
    if existing:
        if existing.group(2) == value:
            return text, False
        return CLOSE_VALUE.sub(r"\1" + value + r"\3", text, count=1), True
    defaults = DEFAULTS_OPEN.search(text)
    if not defaults:
        raise RuntimeError("No profiles.defaults block found; set closeOnExit by hand.")
    after_open = text[defaults.end():]
    close = after_open.find("}")
    if close < 0:
        raise RuntimeError("No profiles.defaults block found; set closeOnExit by hand.")
    body = after_open[:close]
    newline = "\r\n" if "\r\n" in text else "\n"
    indent_match = re.search(r"(?:\r?\n)([ \t]*)[^\s/]", body)
    indent = indent_match.group(1) if indent_match else "            "
    property_text = f'{newline}{indent}"closeOnExit": "{value}"'
    content = re.sub(r"//[^\r\n]*|/\*.*?\*/", "", body, flags=re.DOTALL)
    if content.strip():
        property_text += ","
    return text[:defaults.end()] + property_text + text[defaults.end():], True


def backup_path(path, now=None):
    moment = now or datetime.datetime.now()
    return path.with_name(path.name + ".bak-" + moment.strftime("%Y%m%d%H%M%S"))


def configure(settings_path, value, preview=False, now=None):
    path = Path(settings_path)
    if not path.is_file():
        raise RuntimeError("Windows Terminal settings not found at " + str(path) + ".")
    with path.open("r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    updated, changed = update_close_on_exit(text, value)
    if not changed:
        return "closeOnExit is already '" + value + "'.", None
    if preview:
        return "Would set closeOnExit to '" + value + "' in " + str(path) + ".", None
    backup = backup_path(path, now=now)
    shutil.copy2(path, backup)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    return "closeOnExit set to '" + value + "'. Backup: " + str(backup), backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings-path")
    parser.add_argument("--close-on-exit", choices=CLOSE_VALUES, default="always")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args(argv)
    if not is_windows() and not args.settings_path:
        print("configure-terminal-tab-close: Linux is a no-op; pass --settings-path "
              "to test a disposable Windows Terminal settings file.")
        return 0
    path = Path(args.settings_path) if args.settings_path else default_settings_path()
    message, _backup = configure(path, args.close_on_exit, preview=args.preview)
    print(message)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print("configure-terminal-tab-close: " + str(error), file=os.sys.stderr)
        raise SystemExit(2)
