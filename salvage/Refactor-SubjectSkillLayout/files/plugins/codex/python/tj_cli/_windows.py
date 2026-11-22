from __future__ import annotations

import ctypes
import sys

if sys.platform == "win32":

    def kernel32() -> ctypes.CDLL:
        return ctypes.WinDLL("kernel32", use_last_error=True)

    def last_error() -> int:
        return ctypes.get_last_error()

else:

    def kernel32() -> ctypes.CDLL:
        raise OSError("Windows kernel APIs are unavailable on this platform")

    def last_error() -> int:
        raise OSError("Windows kernel APIs are unavailable on this platform")
