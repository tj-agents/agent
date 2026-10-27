## Codex memory

Use the native Codex executable in the selected `CODEX_HOME` and inspect the `memories` feature:

```text
codex features list
codex features enable memories
codex features disable memories
```

Run only the operation matching the request, then read the native feature state again. `features enable`
and `features disable` persist in that Codex user's `config.toml`; this is a user-configuration change,
not a project-local toggle. If the request permits only a project-local change, report that boundary and
leave the broader setting unchanged. Never assume the feature exists or is enabled on this version, and
never write `.codex/settings.local.json` or `autoMemoryEnabled` as a Codex memory control.

Use [Codex's configuration reference](https://developers.openai.com/codex/config-reference/) for the
current `features.memories` capability and native memory scope.
