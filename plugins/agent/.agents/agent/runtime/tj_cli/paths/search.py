from collections.abc import Iterable


def prepend(entries: Iterable[str], directory: str) -> tuple[str, ...]:
    """Put directory first and retain each literal search entry once.

    Empty entries retain their current-directory meaning. Callers split and join
    with their platform separator and apply their own filtering policy. Filesystem
    normalization would conflate distinct search locations.
    """
    return tuple(dict.fromkeys((directory, *entries)))
