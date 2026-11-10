"""Codex launches start or join the shared app-server daemon unless config stops it."""

from __future__ import annotations

import tomllib

FEATURE = "daemon_auto_start"


def disabled(config: dict[str, object]) -> bool:
    features = config.get("features")
    return isinstance(features, dict) and features.get(FEATURE) is False


def enabled_text(text: str) -> str:
    """`text` without the setting that stops launches starting the shared daemon."""
    before = tomllib.loads(text)
    if not disabled(before):
        return text
    features = {k: v for k, v in before["features"].items() if k != FEATURE}
    expected = comparable({**before, "features": features})
    kept = []
    statement = ""
    table: tuple[str, ...] = ()
    for line in text.splitlines(keepends=True):
        statement += line
        try:
            value = tomllib.loads(statement)
        except tomllib.TOMLDecodeError:
            continue
        if statement.lstrip().startswith("["):
            table = header(value)
            kept.append(statement)
        elif not disabling(table, value):
            kept.append(statement)
        statement = ""
    if statement:
        raise ValueError("Cannot identify complete Codex settings statements")
    result = "".join(kept)
    if comparable(tomllib.loads(result)) != expected:
        raise ValueError(f"Removing {FEATURE} = false would change other settings")
    return result


def disabling(table: tuple[str, ...], value: dict[str, object]) -> bool:
    if table == ("features",):
        return value == {FEATURE: False}
    return table == () and value == {"features": {FEATURE: False}}


def comparable(config: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in config.items() if not (k == "features" and v == {})}


def header(value: object) -> tuple[str, ...]:
    path: list[str] = []
    while isinstance(value, dict) and len(value) == 1:
        key, value = next(iter(value.items()))
        path.append(key)
    return tuple(path)
