import ctypes
import json
import os
import sys
from pathlib import Path


def identity() -> int | list[int]:
    result: int | list[int]
    if sys.platform == "win32":
        processes = (ctypes.c_ulong * 1024)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(processes, len(processes))
        result = list(processes[:count])
    else:
        result = os.getsid(0)
    return result


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(identity()), encoding="utf-8")
