---
name: recent
description: List recent conversations for the selected Codex or Claude profile and return exact native resume commands. Use when asked which conversation to resume or what the last session covered.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: python
provenance: house
---

# Recent conversations

Requires Python 3.9+ and local transcript data for the selected harness. The packaged helper supports
Codex session metadata/response records and Claude project message records. It reads data only; it
never modifies profiles, launches a resume, or uploads history. Data in CODEX_HOME or CLAUDE_CONFIG_DIR
is an external input, not an unshipped helper dependency. Unsupported/empty records are not invented.

For Codex in the current directory, run the helper with `python` on Windows or `python3` on Linux/macOS:

```text
python -B "<package-root>/.agents/agent/runtime/history.py" recent --host codex
```

Use `--host claude` for Claude. Respect the requested harness; otherwise use the current host. Options
are `--count N`, `--cwd <directory>`, `--project <substring>`, `--all-projects`, `--worktree <directory>`,
and an explicit transcript `--history-root <directory>` for a non-default data store. Quote paths and
arguments for the calling shell.

`--worktree <directory>` finds the conversations that worked in a checkout even when they started
elsewhere (a `git -C`/tool call naming it from the main checkout or another directory), searching all
projects; prefer it when asked which conversation owns a worktree. Results add `matched_by` (`cwd` or
`tool`) and sort by `last_activity`, newest first.

Report the relevant session and its returned `resume` command. The newest session may be the current
one; distinguish it using the preview. Never guess an ID or use Claude's resume syntax for Codex.
An absent history directory fails clearly; an empty result means no supported matching conversations.
Only inspect history when the user requested it or authorized conversation enrichment. Never publish
raw history as a test fixture, report attachment or source file.
