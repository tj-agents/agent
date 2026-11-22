"""Configure Windows Terminal profile defaults to close exited tabs (Python 3.9+)."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

CLOSE_VALUES = ("always", "graceful", "automatic", "never")


def is_windows() -> bool:
    return os.name == "nt"


def default_settings_path(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    local_app_data = values.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is not set; pass --settings-path explicitly")
    return (
        Path(local_app_data)
        / "Packages"
        / "Microsoft.WindowsTerminal_8wekyb3d8bbwe"
        / "LocalState"
        / "settings.json"
    )


def _skip_space(text: str, position: int) -> int:
    while position < len(text) and text[position] in " \t\r\n":
        position += 1
    return position


def _jsonc_document(text: str) -> str:
    characters = list(text)
    commas = []
    decoder = json.JSONDecoder()
    position = 0
    if text.startswith("\ufeff"):
        characters[0] = " "
    while position < len(text):
        if text[position] == '"':
            _, position = decoder.raw_decode(text, position)
        elif text.startswith("//", position):
            end = position + 2
            while end < len(text) and text[end] not in "\r\n":
                end += 1
            characters[position:end] = " " * (end - position)
            position = end
        elif text.startswith("/*", position):
            end = text.find("*/", position + 2)
            if end < 0:
                raise RuntimeError(
                    "Invalid Windows Terminal settings JSONC: unclosed comment."
                )
            end += 2
            for index in range(position, end):
                if characters[index] not in "\r\n":
                    characters[index] = " "
            position = end
        else:
            if text[position] == ",":
                commas.append(position)
            position += 1
    document = "".join(characters)
    for comma in commas:
        preceding = comma - 1
        while preceding >= 0 and document[preceding] in " \t\r\n":
            preceding -= 1
        following = _skip_space(document, comma + 1)
        if (
            preceding >= 0
            and document[preceding] not in "{[,:"
            and following < len(document)
            and document[following] in "}]"
        ):
            characters[comma] = " "
    return "".join(characters)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(
                "Ambiguous Windows Terminal settings: duplicate property " + key + "."
            )
        result[key] = value
    return result


def _invalid_constant(value: str) -> object:
    raise RuntimeError(
        "Invalid Windows Terminal settings JSONC: non-JSON constant " + value + "."
    )


def _member_span(document: str, opening: int, name: str) -> tuple[int, int] | None:
    decoder = json.JSONDecoder()
    position = _skip_space(document, opening + 1)
    while document[position] != "}":
        key, end = decoder.raw_decode(document, position)
        start = _skip_space(document, _skip_space(document, end) + 1)
        _, end = decoder.raw_decode(document, start)
        if key == name:
            return start, end
        position = _skip_space(document, end)
        if document[position] == ",":
            position = _skip_space(document, position + 1)
    return None


def update_close_on_exit(text: str, value: str) -> tuple[str, bool]:
    if value not in CLOSE_VALUES:
        raise RuntimeError("Unsupported closeOnExit value: " + value + ".")
    try:
        document = _jsonc_document(text)
        parsed: object = json.loads(
            document, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except (json.JSONDecodeError, RecursionError) as error:
        raise RuntimeError("Invalid Windows Terminal settings JSONC.") from error
    if not isinstance(parsed, dict):
        raise RuntimeError("Windows Terminal settings must be an object.")
    opening = _skip_space(document, 0)
    for name in ("profiles", "defaults"):
        member = _member_span(document, opening, name)
        if member is None or document[member[0]] != "{":
            raise RuntimeError(
                "No profiles.defaults block found; set closeOnExit by hand."
            )
        opening = member[0]
    existing = _member_span(document, opening, "closeOnExit")
    if existing is not None:
        start, end = existing
        current: object = json.loads(document[start:end])
        if not isinstance(current, str):
            raise RuntimeError(
                "profiles.defaults.closeOnExit must be a string; set it by hand."
            )
        if current == value:
            return text, False
        return text[:start] + json.dumps(value) + text[end:], True
    newline = "\r\n" if "\r\n" in text else "\r" if "\r" in text else "\n"
    first_member = _skip_space(document, opening + 1)
    line_start = (
        max(text.rfind("\n", 0, first_member), text.rfind("\r", 0, first_member)) + 1
    )
    prefix = text[line_start:first_member]
    indent = (
        prefix
        if not prefix.strip() and document[first_member] != "}"
        else "            "
    )
    property_text = f'{newline}{indent}"closeOnExit": "{value}"'
    if document[first_member] != "}":
        property_text += ","
    return text[: opening + 1] + property_text + text[opening + 1 :], True


def backup_path(path: Path, now: datetime.datetime | None = None) -> Path:
    moment = now or datetime.datetime.now()
    return path.with_name(path.name + ".bak-" + moment.strftime("%Y%m%d%H%M%S"))


def configure(
    settings_path: str | Path,
    value: str,
    preview: bool = False,
    now: datetime.datetime | None = None,
) -> tuple[str, Path | None]:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings-path")
    parser.add_argument("--close-on-exit", choices=CLOSE_VALUES, default="always")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args(argv)
    if not is_windows() and not args.settings_path:
        print(
            "configure-terminal-tab-close: Linux is a no-op; pass --settings-path "
            "to test a disposable Windows Terminal settings file."
        )
        return 0
    path = Path(args.settings_path) if args.settings_path else default_settings_path()
    message, _backup = configure(path, args.close_on_exit, preview=args.preview)
    print(message)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print("configure-terminal-tab-close: " + str(error), file=sys.stderr)
        raise SystemExit(2) from error
