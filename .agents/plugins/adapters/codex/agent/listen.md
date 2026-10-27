Use the background execution and result-delivery tools advertised by the current Codex surface.
An `exec_command` response containing `session_id` supplies a running command handle; it does not,
by itself, establish a completion notification.

On a surface exposing `functions.exec`, `notify` and nested process tools, keep an awaited relay in
one running cell. Start the caller's command once, retain each returned `session_id`, and use
`write_stdin` inside that cell until the command exits. Call `notify` with the compact result. This
waits in deterministic code while the conversation can continue, without model polling.

```javascript
// @exec: {"yield_time_ms": 1000, "max_output_tokens": 1000}
try {
  let result = await tools.exec_command({
    cmd: "python -B -m unittest discover -s tests -p test_example.py",
    workdir: repositoryRoot,
    max_output_tokens: 1000
  });
  while (result.session_id) {
    result = await tools.write_stdin({
      session_id: result.session_id,
      chars: "",
      max_output_tokens: 1000
    });
  }
  notify({ purpose: "focused regression tests", result });
} catch (error) {
  notify({ purpose: "focused regression tests", error: String(error) });
}
```

Replace `repositoryRoot` and the example command with the caller's exact directory and bounded command.
Use its existing capture wrapper when output must be preserved. Retain the running cell ID as well as
the process handle, and consume its terminal result. Do not leave an unawaited promise: it is discarded
when the cell ends. For a command already running, begin the relay with its existing process handle.

Other surfaces must use their own documented native notification primitive. If this session exposes
only process handles and foreground polling, report that completion delivery is unavailable here;
the shared definition owns that capability boundary.
