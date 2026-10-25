#!/usr/bin/env python3
"""Dispatch a prepared handoff to the required native host launcher."""

import argparse
import importlib.util
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent


def _handler_path(host):
    for root in HERE.parents:
        markers = (root / '.git', root / '.codex-plugin' / 'plugin.json', root / '.claude-plugin' / 'plugin.json')
        path = root / '.agents' / 'agent' / 'resources' / 'handoff' / f'launch_{host}.py'
        if path.is_file() and any(marker.exists() for marker in markers):
            return path
    raise SystemExit(f'The packaged {host} handoff launcher was not found relative to {HERE}.')


def _load_handler(host):
    path = _handler_path(host)
    spec = importlib.util.spec_from_file_location(f'agent_handoff_{host}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description='Hand prepared work to an independent Claude or Codex session.')
    parser.add_argument('--host', choices=('claude', 'codex'), required=True)
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--' not in argv:
        parser.error('separate host selection from launcher arguments with --')
    separator = argv.index('--')
    args = parser.parse_args(argv[:separator])
    remaining = argv[separator + 1:]
    return _load_handler(args.host).main(remaining)


if __name__ == '__main__':
    sys.exit(main())
