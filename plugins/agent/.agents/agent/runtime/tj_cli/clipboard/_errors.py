from tj_cli.errors import TjCliError


class ClipboardError(TjCliError):
    """A copy or verification step failed; the message is the whole report."""
