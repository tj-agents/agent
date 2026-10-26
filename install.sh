#!/usr/bin/env bash
exec python3 -B "$(dirname "$0")/.agents/agent/runtime/install.py" "$@"
