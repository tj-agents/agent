---
name: clip
description: Put text on Tommy's clipboard (Windows, Linux or macOS) so he can paste it straight into Teams, an email or a terminal instead of drag-selecting it out of the transcript. Use when drafting a message for someone to paste, and whenever he says "copy that", "put that on my clipboard", "clip that". NOT for GitHub review comments — review drafts are previewed in the reply and posted from here, never pasted. Applies in every repo, personal and work.
kind: utility
domain: agent
profile: core
applicability: agent sessions
requires: python
provenance: house
---

# clip

Terminal text is awkward to select and picks up stray indentation. Anything Tommy is meant to paste
goes on his clipboard.

## How

Write the exact text to a UTF-8 file in the scratchpad, then run the command with `python3` on Linux/macOS
or `python` on Windows:

```sh
python3 '<skill-directory>/scripts/copy_draft.py' --path '<absolute-path-to-file>'
```

`copy_draft.py` sits beside this file, the same way `handoff-claude` carries its launcher.

The script reads UTF-8, trims trailing blank lines, copies, and reads the clipboard back to verify —
it exits non-zero rather than reporting a success it did not achieve. It prints the character count;
relay that, so a silent failure can't pass as done.

**Never pipe the text through the command line.** Quoting mangles backticks, `$`, curly quotes and
emoji, and a heredoc through PowerShell is worse. The file is the interface.

## What goes on the clipboard

**The message with its markdown intact** — that is the point of copying rather than having him
drag-select. Teams renders markdown in the compose box, so backticks around a service, identity,
resource group, role or flag survive as inline code and make the specifics stand out, and a triple
backtick fence renders a command as a block he can copy straight out. Keep them in.

What stays out is anything that belongs to the transcript rather than the message: no "here's the
message:" preamble, no `>` quoting, no bullet markers you added for readability, no surrounding
fence around the whole thing.

One thing at a time. The clipboard holds one item, so if you drafted a message and a command, copy
the one he asked for and say the other is ready on request. Copying overwrites silently, so do it as
the last step before telling him it is there — a later copy in the same turn strands him pasting the
wrong thing.

**The clipboard is volatile and nothing tells you it changed.** Any copy anywhere on the machine
replaces it — a terminal drag-select most of all, which lands plain text with the terminal's own
wrapping and indentation baked in and no HTML flavour. A message copied several turns ago is not
still there. If he says the backticks did not render, do NOT assume you omitted them: re-copy. The
script writes and verifies off the live clipboard at write time, and exits non-zero on a mismatch —
on Windows both an HTML flavour and a plain flavour; on Linux an HTML flavour by default (via
`wl-copy` or `xclip`), falling back to plain text with the markdown kept intact when only `xsel` is
available, and saying so; on macOS always plain text with the markdown kept intact, because `pbcopy`
has no HTML flavour at all. Either way, a bare "Copied ..." line means it truly landed at that
moment — not that it survived since.

**On Linux, default mode is HTML-only — nothing a plain-text target can read.** `wl-copy`/`xclip`
can set only one clipboard flavour per copy, so the default (rich) copy holds `text/html` and nothing
else; a terminal paste or a plain text field gets nothing useful from it, not a plain fallback. Default
mode is for rich targets — Teams, email. For anything pasted into a terminal or a plain field, use
`--plain-only`, same as for a command (below).

## A command Tommy has to run himself

**Put it on the clipboard. Never print it for him to copy out of the terminal.** Terminal copy is
where wrapping and indentation get baked in, and it silently breaks a long command on paste. This is
the same failure that put a line break into a Teams message mid-sentence.

Use `--plain-only` for a command: no HTML flavour, and backticks stay literal because in a shell they
are syntax, not markup. Show the command in your reply too so he can read it, but the clipboard copy
is the one he runs.

Do not invent a wrapper script to dodge the problem — the clipboard already solves it, and a
throwaway script file is machinery he then has to clean up. Write a script only when the thing is
genuinely worth keeping and re-running.

A command that creates or moves a credential still goes to him to run, and pipes the value straight
to its destination so it never reaches the transcript. Never echo the value, and never run such a
command yourself — a half-finished attempt can leave a live credential stranded.

**Teams messages only, for the HTML flavour.** GitHub review comments (`draft-comment`,
`respond-comments`) are previewed in the reply and posted from here, not pasted, so they never go on
the clipboard.

## Say so in one line

End with a short line naming what is on the clipboard and its size, e.g. "On your clipboard (538
chars)." Not a paragraph about the clipboard.
