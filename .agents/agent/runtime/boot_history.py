"""Read the start of the previous uptime from native operating-system records."""

from __future__ import annotations

import json
import sys
from datetime import datetime

import lifecycle


def previous_boot() -> float:
    if sys.platform == "win32":
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "@(Get-WinEvent -FilterHashtable @{LogName='System'; "
            "ProviderName='Microsoft-Windows-Kernel-General'; Id=12} -MaxEvents 2 | "
            "ForEach-Object { ([xml]$_.ToXml()).Event.EventData.Data | "
            "Where-Object Name -eq 'StartTime' | ForEach-Object { $_.'#text' } }) | "
            "ConvertTo-Json -Compress"
        )
        records = json.loads(
            lifecycle.command(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ]
            )
        )
        if isinstance(records, list) and len(records) == 2:
            return datetime.fromisoformat(records[1]).timestamp()
    elif sys.platform == "linux":
        output = lifecycle.command(
            ["journalctl", "--list-boots", "--no-pager", "--utc"]
        )
        for line in output.splitlines():
            fields = line.split()
            if fields and fields[0] == "-1":
                return datetime.fromisoformat(
                    f"{fields[3]}T{fields[4]}+00:00"
                ).timestamp()
    else:
        raise lifecycle.LifecycleError(f"Recovery is unsupported on {sys.platform}")
    raise lifecycle.LifecycleError(
        "The previous boot is unavailable in the OS boot history; "
        "use --older to explicitly recover all dead sessions"
    )
