import re
from collections.abc import Mapping
from pathlib import Path


def executable(
    process_path: str | None,
    process_environment: Mapping[str, str],
    user_path: object,
    windows: bool,
) -> str | None:
    if not windows:
        return process_path
    paths = [process_environment.get("PATH"), user_path]
    candidates = [Path(process_path)] if process_path else []
    variables = {name.casefold(): value for name, value in process_environment.items()}
    for search_path in paths:
        if not isinstance(search_path, str):
            continue
        for entry in search_path.split(";"):
            directory = (
                re.sub(
                    r"%([^%]+)%",
                    lambda match: variables.get(match[1].casefold(), match[0]),
                    entry,
                )
                .strip()
                .strip('"')
            )
            if directory and "\0" not in directory and Path(directory).is_absolute():
                candidates.append(Path(directory) / "pwsh.exe")
    for candidate in candidates:
        try:
            parts = candidate.parts + candidate.resolve().parts
            packaged = any(part.casefold() == "windowsapps" for part in parts)
            if not packaged and candidate.is_file():
                return str(candidate)
        except (OSError, ValueError):
            continue
    return None
