# Lane selection repair

Work item: https://github.com/tj-agents/agent/issues/175

User direction: use lanes alone to match model capability and thinking effort to each unit of work.
Critical analysis and consequential design can select L1. Routine implementation remains L4.

## Source candidates

- Agent: `Fix/LaneSelection`, commit `10f016af1844bd8f5a801f821fb326e4ff434534`.
- Core: `Fix/LaneSelection`, commit `ca6f905d404d3f59ca2d053d4a72ce067b22f188`.
- Both working checkouts are under their repository's `.worktrees/Fix-LaneSelection`.
- The occupied primary checkouts were preserved.
- Both branches are pushed to `origin/Fix/LaneSelection`; their remote tips match the full commits above.

Handoff on both hosts and `tj start` now require a lane and reject raw model/effort and frontier flags.
The separate frontier table and resolver were removed. Agent and Core handoff guidance now use lanes.
Codex L4 selects `gpt-6.1-terra` at high effort. L1 is Astra at xhigh; L2/L3 are Sol at xhigh/high;
L5/L6/L7 are Luna at high/medium/low. Claude retains its paired effort tiers and L1 routing proxy.

The exact Terra identifier and high effort are documented by OpenAI at
https://developers.openai.com/api/docs/models/gpt-6.1-terra . A fresh native Codex request selected that
L4 table entry at high effort and returned `L4_READY` with exit 0. Its local catalog still lacks the
model's metadata and warned that fallback metadata was used. Installed plugin activation remains pending.

## Validation

- Agent locked dependency synchronization, Ruff formatting/lint, mypy, Template check, generation and
  generation check passed.
- Agent focused runtime/launcher/start tests: 107 passed, 15 existing platform skips, 3 subtests passed.
- Capability contracts plus handoff/start tests: 77 passed.
- Real packaged dispatcher processes rejected missing lanes and model overrides for both hosts with
  exit 2 before launch.
- Core locked synchronization, full Ruff formatting/lint and mypy passed.
- Core generation and generation check passed using the supported local Agent source override.
- Core lane tests: 25 passed, 480 subtests passed.
- Core continuation tests from the earlier combined run passed; its initial lane failures were caused
  by testing before generation and were resolved by regeneration and the passing lane rerun above.
- Core full suite cannot collect: five `fixtures.lane_expectations` import errors. Reproduced on
  unchanged base `6339abcf7ddeb5b5ed257cd677c31367f740aa22` with its pinned dependencies staged.
- Final stable full Agent suite: 771 passed, 20 existing platform skips, 103 subtests passed, exit 0.
  The run completed in 3233.04 seconds; its output is preserved in `FULL_VALIDATION.log`.
- Native Codex review of Agent returned one generated-output finding. It is resolved by the already
  successful local generation/check and rerun against the frozen source candidate. Template's current
  shared CI regenerates before verification and publishes generated output after merge. The regenerated
  frozen review checkout passed 131 tests with 15 existing platform skips and 3 subtests.
- Core native review recursively invoked itself and was stopped. Its configured review-lens fallback
  could not run because its model is unsupported by the current account. Parent review and focused
  tests cover the three source files; independent Core review remains unavailable.

## Remaining activation gate

Core's `AGENTS.md` permits updating `.agents/plugins/runtime-sources.json` only through a Renovate PR
carrying `ci:full`. Its pin has not been edited. The Core candidate requires the new Agent dependency
before activation; local source overrides prove candidate generation, not released installation.
Generated distribution output and digests are left for their owning publication jobs.

## Global policy removal authorized and verified

Tommy explicitly authorized deleting machine-local global policy on November 12, 2026.
Removed C:/Users/TommySeery/.claude/CLAUDE.md, C:/Users/TommySeery/AGENTS.md, and
C:/Users/TommySeery/.codex/AGENTS.md. All three are absent. Alternate home CLAUDE.md,
CLAUDE.local.md, user Claude rules/commands, and Codex AGENTS.override.md were not present.
User host settings contain no extra top-level instruction text or Claude instruction hooks.
Repository-scoped plugin projection and protected-global writer cleanup remain owned by the
coordinating configuration session; it received Tommy's no-global-policy direction.

Other coordinated sessions: `codex:01a1cf7b-e85f-7472-8a9d-7b3ea2684755` and
`codex:01a1cf80-eee2-79e0-9087-3482774639f1`.

## Publication scope assessment

Tommy explicitly requested PR delivery. No existing PR owns either Fix/LaneSelection branch.
Agent is one atomic lane-selection cutover across launchers, resolver, policy and behavior tests.
Core is its separate three-file consumer change; separate repositories require separate reviews.
Both candidates match their remote tips and contain the current origin/main base. Source code is
committed, required local checks are recorded, and host coverage validates for both. The Core PR
stays draft for the Renovate dependency update and independent review. Agent PR: https://github.com/tj-agents/agent/pull/176.

## Pull requests

Agent: https://github.com/tj-agents/agent/pull/176, head 10f016af1844bd8f5a801f821fb326e4ff434534.
Core: https://github.com/tj-agents/core/pull/606, head ca6f905d404d3f59ca2d053d4a72ce067b22f188.
Agent is ready for review with exact-head CI green on Windows and Linux. Core remains draft and requires the Renovate Agent pin update and independent review.
Agent CI exposed one stale PY016 exception after launcher tests stopped touching process state.
Removed that exception; full mechanical Python checker, locked sync, Ruff and all three mypy groups pass.
Runtime source is unchanged from the full 771-pass suite. New exact-head CI is running.
Core Python standards passed on both platforms. Its aggregate gate required ci:full because main is red;
added the required label to run the repository full matrix without changing source or the protected pin.
Issue 175 could not move to In Review: its configured project supports Todo, In Progress, Done only.
The tracked work remains in its current state; PR creation succeeded.

Core exact-head full CI source suite: 546 passed, 6 skips, 489 subtests, one existing Renovate test failure.
The failing test and configuration have identical Git blobs on base and candidate.
Agent incremental review recorded the five-line exception deletion with parent fallback after native review recursed.
Source-review watermark is 10f016af1844bd8f5a801f821fb326e4ff434534. Global instruction-file absence was rechecked.

Final Agent CI: all required checks passed on Windows and Linux at 10f016af1844bd8f5a801f821fb326e4ff434534.
Agent PR 176 marked ready for review; Core PR 606 remains draft. No merge or auto-merge was performed.

## Post-approval continuation — November 12, 2026

Tommy ran `tj approve`; Agent PR 176 merged at 13:58:55 UTC with merge commit
`cc6403e4f68fa7b94c65684a4e31bc22613454ca`. Its causally triggered main CI/publication is run
43232518173. The exact-run monitor used workflow ID `lane-selection-publication`; its foreground
tool session 73488 completed successfully. A direct forge read confirmed all jobs passed and generated
commit `d64a152f30b91cd42f9fb67e0eaf8054854b482f` names the landing commit and has it as its parent.
No open Core runtime-source Renovate PR was present at the post-publication check.

Core PR 606 remains open/draft at `ca6f905d404d3f59ca2d053d4a72ce067b22f188`.
Windows full CI also confirmed the existing Renovate failure: 544 passed, 8 skipped, 489 subtests passed,
one failure in `test_renovate_config_parses_and_has_no_extends`. Linux and Windows Python standards passed.
The protected Agent source pin and independent-review gates remain. This owned Core follow-up prevents
`tj done`; its draft state and source branch must be preserved.
