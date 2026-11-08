"""Process-local Git configuration for native plugin operations."""

import re
from collections.abc import Mapping


def native_plugin_environment(
    environment: Mapping[str, str], *, windows: bool
) -> dict[str, str]:
    """Return an environment where Git accepts long paths on Windows."""
    result = dict(environment)
    if not windows:
        return result

    count_text = result.get("GIT_CONFIG_COUNT", "0")
    if re.fullmatch(r"[0-9]+", count_text) is None:
        raise ValueError("invalid GIT_CONFIG_COUNT")
    try:
        count = int(count_text)
    except ValueError as error:
        raise ValueError("invalid GIT_CONFIG_COUNT") from error

    last_longpaths: str | None = None
    for index in range(count):
        key_name = f"GIT_CONFIG_KEY_{index}"
        value_name = f"GIT_CONFIG_VALUE_{index}"
        key = result.get(key_name)
        if not key:
            raise ValueError(f"invalid Git config entry {index}: missing KEY")
        if value_name not in result:
            raise ValueError(f"invalid Git config entry {index}: missing VALUE")
        if key.casefold() == "core.longpaths":
            last_longpaths = result[value_name]

    if last_longpaths is not None and last_longpaths.casefold() in {
        "true",
        "yes",
        "on",
        "1",
    }:
        return result

    if f"GIT_CONFIG_KEY_{count}" in result or f"GIT_CONFIG_VALUE_{count}" in result:
        raise ValueError(
            f"invalid Git config entry {count}: append slot already occupied"
        )

    result[f"GIT_CONFIG_KEY_{count}"] = "core.longpaths"
    result[f"GIT_CONFIG_VALUE_{count}"] = "true"
    result["GIT_CONFIG_COUNT"] = str(count + 1)
    return result
