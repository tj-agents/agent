## Claude memory

Use Claude Code's native `/memory` interface to inspect loaded instruction and memory files and its
auto-memory control. Follow the native UI for the requested state and scope; when interaction belongs to
the user, identify that exact native operation instead of editing a setting through another host.

```text
/memory
```

Claude's auto-memory control and instruction files have different scopes. Report which native control
was inspected or changed. Background sessions have restricted memory behavior; do not assume they expose
the interactive interface or can write memory. Use the current [Claude memory documentation](https://code.claude.com/docs/en/memory)
for the available interface, scope and background-session limits.
