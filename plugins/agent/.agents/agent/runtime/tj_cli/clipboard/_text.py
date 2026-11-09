import re
from pathlib import Path

from tj_cli.clipboard._errors import ClipboardError

_TRAILING_WHITESPACE = re.compile(r"\s+\Z")


def strip_trailing_whitespace(value: str) -> str:
    return _TRAILING_WHITESPACE.sub("", value)


def normalise_text(raw: str) -> str:
    """CRLF -> LF, then whitespace trimmed off only the very end of the text (not per
    line)."""
    return strip_trailing_whitespace(raw.replace("\r\n", "\n"))


def read_text_file(path: str) -> str:
    """The normalised text, or a ClipboardError naming a missing, non-UTF-8 or empty
    draft file.

    Read as `utf-8-sig` rather than `utf-8` so a leading BOM -- common from Windows
    editors -- is dropped instead of landing on the clipboard as a stray character.
    """
    resolved = Path(path)
    if not resolved.is_file():
        raise ClipboardError(f"Draft file not found: {path}")
    try:
        raw = resolved.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise ClipboardError(f"Draft file is not valid UTF-8: {path}") from None
    text = normalise_text(raw)
    if not text.strip():
        raise ClipboardError(f"Draft file is empty: {path}")
    return text
