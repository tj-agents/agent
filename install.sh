#!/usr/bin/env bash
set -euo pipefail
if ! command -v pwsh >/dev/null 2>&1; then
    echo "tj install requires pwsh" >&2
    exit 2
fi
exec pwsh -NoProfile -File "$(dirname "$0")/install.ps1" "$@"
