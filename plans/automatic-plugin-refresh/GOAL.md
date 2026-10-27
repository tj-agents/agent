# Automatic plugin refresh

Authority: follow the October 27, 2026 handoff from the career session. Deliver reviewed PRs in the owning repositories; Tommy reserves merging. Work from Agent's current checkout. Preserve unrelated `shell/` files and Core's dirty primary checkout.

Outcome: plugins update without interrupting ordinary work, retain project/local scope, and converge once per repository across linked worktrees. Launch repairs stale installed bytes before checking readiness. Background merge events update every registered repository, with obsolete registry entries and unreferenced cache versions pruned safely.

Additional evidence: Codex handoff failed with `Automatic plugin update check is failed after install recovery`; verification reported `Native installed bytes differ from marketplace: python@tj-agents (~/.codex/plugins/cache/tj-agents/python/local)`. Explicit full Codex refresh repaired it. Cover this exact stale-byte case with a regression.

## Assessment and delivery

Reuse the existing standard-library host adapters, event parser, Git common-directory identity, publisher content cache and cache-pruning utility. Native Windows scope behavior was assessed with a disposable local marketplace and isolated configuration. Source inspection and machine task inventory established that no persistent event subscriber is installed: the earlier listener was retired. A small supervised subscriber using the existing channel and reconciler is therefore necessary for background delivery; it needs no third-party dependency or user-scope plugin installation.

Primary references inspected: Claude's official plugin manifest and install/scope documentation (`https://code.claude.com/docs/en/plugins-reference`, `https://code.claude.com/docs/en/discover-plugins`). Repository source and Windows probes determine actual registration behavior. Public native update commands do not batch repository registrations or provide cross-host byte verification; selective reuse of the existing runtime is the smallest suitable approach. Assessment complete; retain these findings across continuation.

Delivery map:

- Core: existing PR 512 (`Fix/TierGateUntrackedScan`), inspected at `1268140a639b649632593f75b2c9ff0b26a4a5fe`, removes the router and every PreToolUse registration. Its `test_no_package_intercepts_tool_calls` rejects reintroduction. This supersedes item 1; no duplicate Core implementation is needed. Tommy retains merge approval for this delivery too.
- Agent parent: `Fix/AutomaticPluginRefresh`, based on fetched `origin/main` at `4fad240`. Batch repository registration and stale-byte launch repair. PR: not opened.
- Agent child: a dependent branch and PR for the persistent background subscriber and installer supervision. PR: not opened. It depends on the parent's batch updater, and is deliberately separated to keep scope reviewable. The user merge hold prevents landing the parent before developing the dependent slice. Measure substantive code and tests separately from generated mirrors.

## Acceptance

1. Router cannot block a tool on an unavailable skill, and router timeouts cannot deny it; verify existing Core removal first.
2. Native Windows inventory observes the same current plugin from primary, nested and sibling worktrees while another repository retains its own selection.
3. Launch and explicit refresh repair stale project/local installs and Codex installed-byte mismatches, then verify the resulting bytes. No user-scope widening.
4. Supervised machine-event subscriber reuses the existing channel and reconciler to update all registered repositories once, with no model process or close/resume watcher involved in background updates.
5. Batch registry writes and publisher fetches; prune missing project paths and unreferenced caches while retaining active-session pins.
6. Focused tests, generator freshness, required review and Windows/Linux CI; open PRs and retain the explicit user merge hold.

## Current state

Implemented batch scoped registration and Codex installed-byte reconciliation. The isolated native Windows CLI readback observes version 2.0.0 enabled in both primary and sibling linked worktrees, disabled in a foreign repository, from one project registration. The first 109 focused regressions passed, including the exact stale `python/local` launch case. Additional dead-path and Unix executable-permission checks are being validated; the full Windows suite is running. Existing Agent source adapters are authored in `.claude/runtime` and `.codex/runtime`, then packaged as runtime `claude` and `codex` modules. Run tests against regenerated packages; directly importing authored shared runtime omits those packaged adapters.

Next action: finish required validation, commit and independently review the parent, open its draft PR, then implement the dependent background subscriber. Both Agent PRs and Core PR 512 must remain unmerged pending Tommy.

```agent-host-coverage
{"schema_version":1,"shared_source":".agents/agent/runtime/launch_check.py","hosts":[{"host":"claude","behavior":"updates repository-scoped plugin registrations before launch","source":".claude/runtime/plugins.py","mapping":"Agent packaged claude runtime adapter","verification":{"level":"installed","result":"passed","evidence":"isolated native Windows CLI: primary and sibling enabled version 2.0.0; foreign repo disabled; one project registration; TEMP/agent-batch-native-probe.log"}},{"host":"codex","behavior":"repairs stale installed bytes before launch readiness","source":".codex/runtime/plugins.py","mapping":"Agent packaged codex runtime adapter","verification":{"level":"source","result":"passed","evidence":"test_batch_plugin_refresh exact python/local mismatch during launch with unchanged published revision memo"}}]}
```
