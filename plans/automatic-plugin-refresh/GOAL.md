# Automatic plugin refresh

Authority: follow the October 27, 2026 handoff from the career session. Deliver reviewed PRs in the owning repositories; Tommy reserves merging. Work from Agent's current checkout. Preserve unrelated `shell/` files and Core's dirty primary checkout.

Outcome: plugins update without interrupting ordinary work, retain project/local scope, and converge once per repository across linked worktrees. Launch repairs stale installed bytes before checking readiness. Background merge events update every registered repository, with obsolete registry entries and unreferenced cache versions pruned safely.

Additional evidence: Codex handoff failed with `Automatic plugin update check is failed after install recovery`; verification reported `Native installed bytes differ from marketplace: python@tj-agents (~/.codex/plugins/cache/tj-agents/python/local)`. Explicit full Codex refresh repaired it. Cover this exact stale-byte case with a regression.

## Assessment and delivery

Reuse the existing standard-library host adapters, event parser, Git common-directory identity, publisher content cache and cache-pruning utility. Native Windows scope behavior was assessed with a disposable local marketplace and isolated configuration. Source inspection and machine task inventory established that no persistent event subscriber is installed: the earlier listener was retired. A small supervised subscriber using the existing channel and reconciler is therefore necessary for background delivery; it needs no third-party dependency or user-scope plugin installation.

Primary references inspected: Claude's official plugin manifest and install/scope documentation (`https://code.claude.com/docs/en/plugins-reference`, `https://code.claude.com/docs/en/discover-plugins`). Repository source and Windows probes determine actual registration behavior. Public native update commands do not batch repository registrations or provide cross-host byte verification; selective reuse of the existing runtime is the smallest suitable approach. Assessment complete; retain these findings across continuation.

Delivery map:

- Core: existing PR 512 (`Fix/TierGateUntrackedScan`), inspected at `1268140a639b649632593f75b2c9ff0b26a4a5fe`, removes the router and every PreToolUse registration. Its `test_no_package_intercepts_tool_calls` rejects reintroduction. This supersedes item 1; no duplicate Core implementation is needed. Tommy retains merge approval for this delivery too.
- Agent parent: `Fix/AutomaticPluginRefresh`, based on fetched `origin/main` at `4fad240`. Batch repository registration and stale-byte launch repair. PR: https://github.com/tj-agents/agent/pull/27 (draft; user merge hold).
- Agent child: `Feature/BackgroundPluginEvents`, based on parent `60ba854a8ca1c66ee86e4053cd32421a93d19adf`, for the persistent background subscriber, installer supervision and existing-machine bootstrap. PR: https://github.com/tj-agents/agent/pull/28 (draft; user merge hold). The explicit user merge hold prevents landing the parent before completing the dependent slice.
- Producer dependency: existing Agent PR 20 (`Fix/PluginPublishHints`, head `fdb8862b484b402864af8f0f92d733cce6439ff2`) supplies Agent main-push hints and passed Windows, Ubuntu and host CI. Existing channel enrollment in install configures publisher variables/secrets with Tommy's saved login. Preserve this separate review; do not duplicate its workflow or claim its unmerged producer is active.

## Acceptance

1. Router cannot block a tool on an unavailable skill, and router timeouts cannot deny it; verify existing Core removal first.
2. Native Windows inventory observes the same current plugin from primary, nested and sibling worktrees while another repository retains its own selection.
3. Launch and explicit refresh repair stale project/local installs and Codex installed-byte mismatches, then verify the resulting bytes. No user-scope widening.
4. Supervised machine-event subscriber reuses the existing channel and reconciler to update all registered repositories once, with no model process or close/resume watcher involved in background updates.
5. Batch registry writes and publisher fetches; prune missing project paths and unreferenced caches while retaining active-session pins.
6. Focused tests, generator freshness, required review and Windows/Linux CI; open PRs and retain the explicit user merge hold.

## Current state

Parent `60ba854a8ca1c66ee86e4053cd32421a93d19adf` passed native incremental review and independent review with no remaining findings. The native reviewer ran 116 focused tests with two Unix permission skips; the independent reader ran 78 with the same two skips. Exact-head Windows, Ubuntu and host CI all passed. Private canonical review history records the original dependency activation, stale publisher head, local content/mode, Git fallback, first-fetch offline reuse and shared-payload mode findings and their dispositions.

The isolated native Windows CLI readback observes version 2.0.0 enabled in primary and sibling linked worktrees, disabled in a foreign repository, from one project registration. A separate native probe confirms dependency and dependent enabled at project scope with no user selection. The exact stale `python/local` Codex launch failure has a regression with an unchanged revision memo. Authored shared and host runtime is verified through generated `plugins/agent` packages.

The child implements a single supervised subscriber with durable cursor/repository state, authoritative GitHub queries, reconnect/catch-up and failed-event retries. Managed runtime replacement exits for supervisor restart. Windows uses a hidden current-user task; Linux uses a systemd user service. Installer checks do not write; registration validates ownership and refuses conflicting services. A managed-launch bootstrap upgrades existing machines once, bounds failed setup retries, and leaves foreground refresh functional. No native service was installed during tests. Native Windows Task Scheduler accepted the generated XML in a disposable task definition without registration. The original 32 subscriber/supervisor tests passed. The cumulative Windows run completed 985 tests with 27 skips and one unchanged session-store Git fixture timeout; its isolated rerun passed in 1.1 seconds. Generator freshness and both directions of tracked instruction-file pairing pass. Original child 799f773 passed Windows, Ubuntu and host CI. Native review found bootstrap subprocess-timeout handling, supervision starting before synchronous install convergence, and missing shell tool paths in the Linux service. All three are repaired with regressions; the corrected focused run passed 165 tests with three platform skips. The corrected head still requires delta review and fresh CI.

Child split assessment: 1030 substantive added/deleted lines across runtime and tests, with generated mirrors and this documentation reported separately. The exposed subscriber, matching supervisor arguments/receipt, installer and existing-machine bootstrap must agree to deliver working behavior. A service-only preparation would expose no usable update behavior; splitting runtime/installer would either omit upgrades of existing machines or start a service without its runtime contract. Keep this bounded atomic slice, independently review its event/security and supervisor boundaries, validate cumulative Windows/Linux CI, and add no unrelated scope.

Parent review recovery remains in the original frozen scope: its initial candidate was below the substantive-line threshold; corrections exposed missing activation, local source/mode invalidation, Git fallback and first-fetch revision retention. These repair the published behavior and cannot be deferred to another landable PR. The subscriber remains a separate child.

Next action: publish the reviewed child corrections, independently review that fixed delta, and require exact-head CI before readiness. Parent PR 27 is ready and unmerged; the child stays based on parent 60ba854. Mark ready only when those gates pass. Both Agent PRs and Core PR 512 remain unmerged pending Tommy. Agent PR 20 is a separate producer dependency.

```agent-host-coverage
{"schema_version":1,"shared_source":".agents/agent/runtime/launch_check.py","hosts":[{"host":"claude","behavior":"updates repository-scoped plugin registrations before launch","source":".claude/runtime/plugins.py","mapping":"Agent packaged claude runtime adapter","verification":{"level":"installed","result":"passed","evidence":"isolated native Windows CLI: primary and sibling enabled version 2.0.0; foreign repo disabled; one project registration; TEMP/agent-batch-native-probe.log"}},{"host":"codex","behavior":"repairs stale installed bytes before launch readiness","source":".codex/runtime/plugins.py","mapping":"Agent packaged codex runtime adapter","verification":{"level":"source","result":"passed","evidence":"test_batch_plugin_refresh exact python/local mismatch during launch with unchanged published revision memo"}}]}
```

The child review corrections remain inside the frozen subscriber scope. The timeout, installer-order and inherited-PATH defects prevent the originally published behavior working reliably. Keep their fixes and regressions with PR 28 rather than creating incomplete preparatory API layers. Generated mirrors account for 468 changed lines, separately from source/tests and 25 documentation lines at correction preparation.
