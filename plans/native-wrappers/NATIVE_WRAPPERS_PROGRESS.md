# Native Agent wrappers progress

- Plan: plans/native-wrappers/NATIVE_WRAPPERS_PLAN.md
- Roadmap: plans/native-wrappers/NATIVE_WRAPPERS_ROADMAP.md
- Roadmap item: native-wrappers/agent

Branch `Refactor/NativeWrappers` starts at fresh `origin/main` `438783a2ba1049f2afbecc16d62677cb4b8fd1ac`. The central Docs plan owns whole-goal approval and scope. This ledger owns only the Agent implementation slice.

## Next Steps

Scope: whole plan for this Agent slice; the full cross-repository plan remains incomplete.
Current slice: native message/peer/memory adapters, Codex helper relocation and dead close removal are implemented. Targeted tests pass (42), generated outputs current (282 files), guidance examples and diff checks pass; the full suite ran 949 tests with 22 failures, one error and 25 skips. The 22 Store failures reproduce in the unchanged audit checkout; hook-trust passed all 28 tests alone. A minimal fixture proves unittest.mock.patch.dict restores empty Python environment values as missing native Windows values, making Git reject GIT_CONFIG_VALUE_1. Base CI run40528976052 executed and passed Windows/Linux repository suites and native-host admission; this candidate still needs its own CI. D4 extraction requires preserving live recovery/startup identity proofs and a coordinated CLI API, so no terminal implementation was removed.
Remaining scope: frozen review, remediation and Agent PR delivery with exact-head Windows/Linux CI; conditional terminal extraction and installed acceptance remain explicit.
Done when: the Agent slice passes its acceptance and its PR is delivered; keep every session open.

## Evidence and limits

The message helper relocation preserves its Git blob exactly. Source caller inspection found only the retired close entry calls the retired observer wrapper; the staging regression now exercises the live detached primitive. Protected Agent #22 advanced to `03aa0472bf38959960d6787cb3d424d6544a7e8d`; its permission/startup files remain untouched. Read-only native commands and deterministic fixtures are observed; no live message, memory toggle, installed plugin change or session closure was performed. Fresh installed Windows/Linux host acceptance remains pending and must not be called passed.
