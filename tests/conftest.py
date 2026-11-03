import os
import sys
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def preserve_empty_process_environment() -> Iterator[None]:
    empty = [key for key, value in os.environ.items() if value == ""]
    yield
    if sys.platform == "win32" and empty:
        import ctypes

        # Environment mocks can restore Python's map but delete native empty values.
        set_variable = ctypes.WinDLL(
            "kernel32", use_last_error=True
        ).SetEnvironmentVariableW
        set_variable.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p)
        set_variable.restype = ctypes.c_int
        for key in empty:
            if os.environ.get(key) == "" and not set_variable(key, ""):
                raise ctypes.WinError(ctypes.get_last_error())
