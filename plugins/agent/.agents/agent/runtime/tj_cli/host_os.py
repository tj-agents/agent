"""This machine's operating system and processor, read without a WMI query.

`platform.system()` and `platform.machine()` query WMI on Windows, which hangs while
the WMI service is unhealthy; a hook that asks then outlives its host's timeout.
These return the same names from the interpreter and `os.uname()`.
"""

from __future__ import annotations

import os
import sys
import sysconfig

WINDOWS_MACHINES = {"win-amd64": "AMD64", "win-arm64": "ARM64", "win32": "x86"}


def system() -> str:
    """`platform.system()`'s name: "Windows", "Linux", "Darwin" or the uname sysname."""
    if sys.platform == "win32":
        name = "Windows"
    elif sys.platform.startswith("linux"):
        name = "Linux"
    elif sys.platform == "darwin":
        name = "Darwin"
    else:
        name = os.uname().sysname
    return name


def machine() -> str:
    """`platform.machine()`'s name, such as "AMD64", "ARM64", "x86_64" or "aarch64"."""
    if sys.platform == "win32":
        # The interpreter's build tag: "win-amd64", "win-arm64" or "win32".
        name = WINDOWS_MACHINES[sysconfig.get_platform()]
    else:
        name = os.uname().machine
    return name
