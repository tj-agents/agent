Codex starts the shipped listener, which owns one detached command and returns its compact result to the exact current thread through Codex's native app-server input. Run it with Python 3.9+ after replacing the placeholders; the command begins after `--` and is never a shell string:

```text
python -B "<skill-directory>/scripts/codex_listener.py" start --purpose "exact condition" --timeout 7200 -- python -B .agents/workflows/workflow_ops.py --workflow-run-id <id> monitor --kind pr --id <pr> --head <sha>
```

Keep the returned listener ID. `status --listener-id <id>` reads its durable state. Do not use a foreground `gh ... --watch`, polling loop, or an ordinary background shell process for a deferred delivery decision.
