---
name: refresh-plugins
description: Bring this CLI's tj-agents plugins up to date and into the running session. Run it yourself, without asking, whenever plugins look outdated - a skill or hook is missing or behaves like an older version, or a plugin PR just merged - or when asked to sync, reload, refresh or update the plugins.
kind: utility
domain: agent
profile: core
applicability: native host sessions
requires: python, native host
provenance: house
route: infer
---

# Refresh plugins

Every plugin is installed once per machine and shared by every project. This updates those installs on both hosts
from the marketplaces, updates the native `tj` binaries and shell profiles, restarts background Claude sessions, and names the open
sessions still on the old plugins. Run it on your own judgement; never ask first.

```text
python -B <skill-directory>/scripts/refresh_plugins.py [--host claude|codex]   # python3 on Linux/macOS
```

A failed step is reported and the rest still run; exit 1 lists the failures. New sessions load the result. An open
Claude session needs a human `/reload-plugins`; an open Codex session needs `codex resume <id>` for hook changes.
Tell Tommy which sessions the notice names.
