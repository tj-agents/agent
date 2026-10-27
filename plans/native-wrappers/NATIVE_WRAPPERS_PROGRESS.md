# Native Agent wrappers progress

- Plan: plans/native-wrappers/NATIVE_WRAPPERS_PLAN.md
- Roadmap: plans/native-wrappers/NATIVE_WRAPPERS_ROADMAP.md
- Roadmap item: native-wrappers/agent

Branch `Refactor/NativeWrappers` starts at fresh `origin/main` `438783a2ba1049f2afbecc16d62677cb4b8fd1ac`. The central Docs plan owns whole-goal approval and scope. This ledger owns only the Agent implementation slice.

## Next Steps

Scope: whole plan for this Agent slice; the full cross-repository plan remains incomplete.
Current slice: remove the redundant memory skill and both host adapters after Tommy clarified that native capabilities need no wrapper solely for a shared name. The old incorrect JSON writer remains deleted. Generated outputs are current (277 files), the native runtime-boundary test passes, and a source/package scan confirms both memory names and all host entries are absent. The earlier source candidate passed Windows/Linux CI at `60da803c1dc8e5fe192f494b86fa9c8d1ffaa652`; the corrected candidate requires its own review and exact-head CI.
Remaining scope: frozen incremental review, correction delivery to Agent #26 and exact-head Windows/Linux CI; conditional terminal extraction and installed acceptance remain explicit.
Done when: the Agent slice passes its acceptance and its PR is delivered; keep every session open.

## Evidence and limits

The message helper relocation preserves its Git blob exactly. Source caller inspection found only the retired close entry calls the retired observer wrapper; the staging regression now exercises the live detached primitive. Protected Agent #22 advanced to `03aa0472bf38959960d6787cb3d424d6544a7e8d`; its permission/startup files remain untouched. Read-only native commands and deterministic fixtures are observed; no live message, memory toggle, installed plugin change or session closure was performed. Fresh installed Windows/Linux host acceptance remains pending and must not be called passed.

## Wrapper value audit

Keep message for authorized exact-recipient binding, uncertain-delivery handling and the Codex-only verified-delivery requirement. Keep peer for registered cross-host and physical-terminal identity. Keep listener for one-owner completion delivery without duplicate command execution. Keep history/recovery/open and plugin selection/refresh for their additional cross-host ownership, roster, trust and live-session requirements. Native controls alone suffice for memory; retain no memory skill or replacement alias. The shared authoring rule is corrected in Core's existing PR #517.
