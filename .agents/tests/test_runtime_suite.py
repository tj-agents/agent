"""Expose the migrated stdlib runtime tests to shared plugin CI."""

from pathlib import Path
import unittest


def load_tests(loader, tests, pattern):
    return unittest.TestLoader().discover(str(Path(__file__).resolve().parents[2] / "tests"), pattern="test_*.py")
