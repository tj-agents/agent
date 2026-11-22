#!/usr/bin/env bash
exec python3 -B "$(dirname "$0")/plugins/agent/hooks/install.py" "$@"
