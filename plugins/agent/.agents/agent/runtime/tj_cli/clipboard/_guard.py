import os

from tj_cli.clipboard._errors import ClipboardError


def refuse_real_backend_in_tests(using_real_backend: bool) -> None:
    """Raises when a caller is about to touch the real system clipboard while a test is
    running.

    `PYTEST_CURRENT_TEST` is set by pytest for the duration of each test; a caller still
    using this module's real default backend under that marker is refused outright --
    the same class of bug as a test opening a real browser tab. A caller that injected
    its own backend (a fake `run`/`which`, or its own `windows_api`) is never real, so
    this never fires for it.
    """
    if using_real_backend and "PYTEST_CURRENT_TEST" in os.environ:
        raise ClipboardError(
            "Refusing to use the real system clipboard: PYTEST_CURRENT_TEST is set, so "
            "a test would touch the live clipboard. Inject a fake backend instead."
        )
