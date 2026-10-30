"""Keep the developer's injected git configuration out of test git processes."""

import os

_COUNT = "GIT_CONFIG_COUNT"
_ENTRIES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def _is_git_config(name):
    return name == _COUNT or name.startswith(_ENTRIES)


def without_git_config(environment):
    return {name: value for name, value in environment.items() if not _is_git_config(name)}


def isolate_git_config():
    # Windows drops empty variables when mock.patch.dict restores os.environ, leaving GIT_CONFIG_COUNT pointing
    # at missing GIT_CONFIG_VALUE_n entries and failing every later git call, so remove them for the process.
    for name in tuple(os.environ):
        if _is_git_config(name):
            os.environ.pop(name)
