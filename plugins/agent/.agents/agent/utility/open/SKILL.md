---
name: open
description: Bring back any Claude or Codex CLI session from a session id, a topic or a directory, without choosing the host. A session that is still running has its existing terminal tab focused; a closed one is reopened in its own host and recorded directory. Use when asked to open, reopen, resume, switch to, find the window of or bring back a CLI, agent or conversation.

kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: python, native host
provenance: house
route: infer
---

# Open a CLI session

The caller names the session, never the host. `agent_recovery.py reopen` searches Claude and Codex history,
picks the one session, then either focuses its live tab or reopens it with the native launcher. Do not
select a host-specific skill for a user request that names a session.

Run with `python` on Windows or `python3` on Linux/macOS:

```sh
python -B '<package-root>/.agents/agent/runtime/agent_recovery.py' reopen --target '<session-id | topic | directory>'
```

- `--target` is an exact native session id, a case-insensitive topic matched against ids, tab titles and
  messages, or a directory whose sessions are searched. An exact id wins over a topic. A directory must be
  written as a path (absolute, or containing a separator, or `.`); a bare word is always a topic.
- More than one match is refused with the candidates listed. Pass the exact id, or `--latest` to take the
  most recently active match when the user asked for "the last one". A live match always wins over a more
  recent closed one, so `--latest` never starts a second writer beside a running session on the same topic.
- `--host claude|codex` narrows a search. It is needed only when one id exists in both hosts' profiles.
- `--prompt` or `--prompt-path` seeds a reopened session's first turn and is ignored when a live tab is
  focused. Use the packaged task-recovery resource for recovery prompts.
- `--claude-history-root` and `--codex-history-root` select an alternate profile.

The JSON result carries `action`: `focused` for a verified-live session and `opened` for a reopened one.

## Live sessions are focused, never duplicated

A session whose recorded process is verified live is never launched a second time, because two writers on
one conversation corrupt it. Its registered terminal tab is focused instead (Windows Terminal, tmux, kitty).
If the tab cannot be focused the command fails and names why; report that to the user rather than
reopening. Focus needs the tab the session registered at startup, so a session started before the
registry hook existed has none, and a Windows Terminal tab whose title the CLI has since rewritten may not
be found. A session whose liveness is unknown is refused until the user confirms it has no live writer, then
pass `--confirm-closed`. After an ambiguous terminal result (exit 3), check the opened tab and do not retry
while it may still run. Its launch record releases itself once that tab's process exits; a record that
predates this tracking needs `--confirm-closed` after the user confirms no tab runs the session.
