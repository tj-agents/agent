import ctypes
import json
import os
import sys
from pathlib import Path


def identity() -> int | list[int]:
    if sys.platform != "win32":
        return os.getsid(0)
    processes = (ctypes.c_ulong * 1024)()
    count = ctypes.windll.kernel32.GetConsoleProcessList(processes, len(processes))
    return list(processes[:count])


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(identity()), encoding="utf-8")
