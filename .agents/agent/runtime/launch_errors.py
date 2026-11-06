"""Failures at the native process and terminal launch boundary."""


class LaunchError(Exception):
    pass


class LaunchTimeout(LaunchError):
    pass
