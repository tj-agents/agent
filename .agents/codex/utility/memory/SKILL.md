---
name: memory
description: Read or toggle Codex auto-memory for the current project. Use only for Codex memory settings.
kind: utility
domain: agent
profile: core
applicability: codex sessions
requires: python
provenance: house
---

# Codex memory

Owns Codex auto-memory state for one project. This is Codex-only and changes no other project setting.

```text
python -B "<skill-directory>/scripts/memory.py" status
python -B "<skill-directory>/scripts/memory.py" toggle --project "<exact-project>"
```

Use `status` before reporting state. `toggle` preserves every unrelated JSON key and takes effect in a new Codex session. Report the returned JSON; do not claim it controls instructions or other memory systems.
