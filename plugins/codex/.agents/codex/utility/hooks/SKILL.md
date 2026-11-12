---
name: hooks
description: Disable or restore Codex hooks for one repository while preserving its previous feature gate.
kind: utility
domain: agent
profile: core
applicability: codex sessions
requires: python, codex
provenance: house
route: infer
---

# Codex hook control

Use this utility when hook execution must be stopped before any hook bootstrap runs. Supply the exact target repository root. It changes that repository's
canonical `features.hooks` setting and records the exact previous value in an owned adjacent snapshot.
Run its commands with `python` on Windows or `python3` on Linux/macOS.

```sh
python -B '<skill-directory>/scripts/hook_control.py' status --project '<repository-root>'
python -B '<skill-directory>/scripts/hook_control.py' off --project '<repository-root>'
python -B '<skill-directory>/scripts/hook_control.py' on --project '<repository-root>'
```

The utility rejects global configuration, redirected paths and hard links before writing.
Snapshots stay beside the repository configuration and apply only to that checkout.

`on` restores only a value previously saved by this utility. It never enables every hook. Restart Codex after
changing the setting; no hot reload is established. Status reports the saved layer and cannot verify the effective
state. Project, profile, command-line, or administrator settings can override this repository layer. A project
`.codex/config.toml` applies only when Codex trusts that repository; new or changed project hook definitions still
require hook trust. The gate covers ordinary native lifecycle hooks; builtin cleanup and legacy notify behavior are
outside it.
The utility requires Python 3.12 or newer.
