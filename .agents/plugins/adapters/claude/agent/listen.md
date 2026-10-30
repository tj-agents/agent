Use Claude Code's native `Monitor` tool for a condition that can produce events, such as a PR or CI
status change. Give it the caller's existing bounded watcher command, using the current tool schema.
Each output line arrives in the conversation as an event. Retain the returned monitor identity and
deadline; handle its expiry notice through the owning task.

For a command whose result is needed only when it exits, use the native shell tool with
`run_in_background: true`. Retain the returned task ID and output path, then read that exact output
on completion. The shell tool owns background execution; do not append shell background syntax.

```json
{"command":"python -B -m unittest discover -s tests -p test_example.py","description":"Run the focused regression tests","run_in_background":true}
```

The example uses `python` on Windows; use `python3` for the command on Linux.

Check the tools exposed by this session before invoking them. The [Claude Code tools reference](https://code.claude.com/docs/en/tools-reference#monitor-tool)
documents Monitor events, native shell background execution and their lifetime limits.
