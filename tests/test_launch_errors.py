import subprocess
import sys

import pytest
from launch_errors import LaunchError, LaunchTimeout, cleanup_failed_launch


@pytest.mark.parametrize(
    "secondary",
    [
        LaunchError("cleanup launch failed"),
        OSError("cleanup IO failed"),
        subprocess.SubprocessError("cleanup process failed"),
        RuntimeError("cleanup native failed"),
    ],
)
def test_cleanup_failed_launch_preserves_original_timeout(secondary: Exception) -> None:
    primary = LaunchTimeout("original ambiguous launch")

    def cleanup() -> None:
        raise secondary

    with pytest.raises(LaunchTimeout, match="original ambiguous launch") as failure:
        try:
            raise primary
        finally:
            cleanup_failed_launch(sys.exception(), cleanup)
    assert failure.value is primary
    assert primary.__notes__ == [
        f"Launch cleanup failed ({type(secondary).__name__}): {secondary}"
    ]


def test_cleanup_failed_launch_propagates_cleanup_without_original_failure() -> None:
    secondary = OSError("cleanup failed without original")

    def cleanup() -> None:
        raise secondary

    with pytest.raises(OSError, match="without original") as failure:
        cleanup_failed_launch(None, cleanup)
    assert failure.value is secondary
