import ctypes
import time
from collections.abc import Callable
from typing import Any

from tj_cli.clipboard._errors import ClipboardError
from tj_cli.clipboard._guard import refuse_real_backend_in_tests
from tj_cli.clipboard._text import strip_trailing_whitespace

GMEM_MOVEABLE = 0x0002
CF_UNICODETEXT = 13
HWND_MESSAGE = -3

type WindowsApiFunction = Callable[[], tuple[Any, Any]]


def _windows_api() -> tuple[Any, Any]:
    # Imported and bound lazily: ctypes.windll only exists on Windows, and this module
    # must still import cleanly on Linux/macOS so the pure functions and the POSIX
    # backend stay testable there.
    import ctypes.wintypes as wintypes

    windll: Any = getattr(ctypes, "windll")  # noqa: B009 -- ctypes.windll does not exist off Windows
    user32 = windll.user32
    kernel32 = windll.kernel32

    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL

    # HANDLE/HGLOBAL are explicitly pointer-sized (wintypes.HANDLE is c_void_p) rather
    # than a 32-bit DWORD, so these calls stay correct on 64-bit Windows.
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalSize.argtypes = [wintypes.HANDLE]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.restype = wintypes.HANDLE

    return user32, kernel32


def _create_message_window(user32: Any) -> Any:
    """A hidden message-only window, so SetClipboardData has a real owner to attach the
    data to.

    OpenClipboard(NULL) leaves the clipboard with no owning window at all;
    EmptyClipboard followed by SetClipboardData with no owner is exactly the shape that
    has been seen failing SetClipboardData, so a throwaway `STATIC`-class window
    parented to HWND_MESSAGE stands in as the owner instead. Raises before the caller
    ever opens or empties the clipboard, rather than falling back to
    OpenClipboard(NULL): a copy that cannot get a real owner is better refused than
    silently landed without the ownership semantics the rest of this backend was written
    to rely on.
    """
    import ctypes.wintypes as wintypes

    hwnd = user32.CreateWindowExW(
        0, "STATIC", "", 0, 0, 0, 0, 0, wintypes.HWND(HWND_MESSAGE), None, None, None
    )
    if not hwnd:
        raise ClipboardError(
            "CreateWindowExW failed while preparing a clipboard owner window."
        )
    return hwnd


def _open_clipboard(
    user32: Any, hwnd: Any, retries: int = 10, delay: float = 0.05
) -> None:
    # Another process (often the previous clipboard owner tearing down) can hold the
    # clipboard for a moment; a short retry loop rides that out instead of failing a
    # copy that would succeed a beat later.
    for _ in range(retries):
        if user32.OpenClipboard(hwnd):
            return
        time.sleep(delay)
    raise ClipboardError("Could not open the clipboard; another process held it.")


def _set_clipboard_bytes(
    user32: Any, kernel32: Any, clipboard_format: int, data: bytes
) -> None:
    size = len(data)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.c_size_t(size))
    if not handle:
        raise ClipboardError("GlobalAlloc failed while preparing clipboard data.")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise ClipboardError("GlobalLock failed while preparing clipboard data.")
    try:
        ctypes.memmove(pointer, data, size)
    finally:
        kernel32.GlobalUnlock(handle)
    # Ownership passes to the system only once SetClipboardData succeeds; on failure
    # this call still owns the handle and must free it, or it leaks.
    if not user32.SetClipboardData(clipboard_format, handle):
        kernel32.GlobalFree(handle)
        raise ClipboardError("SetClipboardData failed.")


def _read_windows_clipboard(user32: Any, kernel32: Any, hwnd: Any) -> str | None:
    _open_clipboard(user32, hwnd)
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        size = kernel32.GlobalSize(handle)
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            data = ctypes.string_at(pointer, size - size % 2)
        finally:
            kernel32.GlobalUnlock(handle)
        return data.decode("utf-16-le").split("\x00", 1)[0]
    finally:
        user32.CloseClipboard()


def copy_windows(text: str, *, windows_api: WindowsApiFunction = _windows_api) -> str:
    """Copy `text` on Windows as CF_UNICODETEXT via ctypes, then verify it off the live
    clipboard.

    `windows_api` takes the place of `run`/`which` on the POSIX backend: inject a fake
    returning a stand-in `(user32, kernel32)` pair so a test never touches the real
    clipboard.
    """
    refuse_real_backend_in_tests(windows_api is _windows_api)
    user32, kernel32 = windows_api()
    # Raises before the clipboard is ever opened or emptied if no owner window can be
    # created.
    hwnd = _create_message_window(user32)
    try:
        _open_clipboard(user32, hwnd)
        try:
            if not user32.EmptyClipboard():
                raise ClipboardError("EmptyClipboard failed.")
            _set_clipboard_bytes(
                user32, kernel32, CF_UNICODETEXT, text.encode("utf-16-le") + b"\x00\x00"
            )
        finally:
            user32.CloseClipboard()
        landed = _read_windows_clipboard(user32, kernel32, hwnd)
    finally:
        user32.DestroyWindow(hwnd)
    if strip_trailing_whitespace(landed or "") != text:
        raise ClipboardError(
            "Clipboard verification failed: the text did not round-trip."
        )
    return f"Copied {len(text)} chars."
