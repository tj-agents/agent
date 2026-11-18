## Why

Independent launches could bypass lane selection with raw model, effort and frontier flags. The lane guidance also imposed extra selection machinery instead of matching reasoning capability to each unit of work.

Closes #175.

## What

- Require L1–L6 for both host handoff launchers and `tj start`; resolve model and effort together from the lane tables.
- Remove raw selection overrides and the separate frontier path. Reject invalid lanes and host routes before creating work items or launching sessions.
- Simplify lane guidance around the reasoning required, including critical analysis and consequential design at L1, and update Codex L4 to the verified current model.
- Cover table pair propagation, rejected overrides, host routing and preserved launch metadata with focused tests.

## Validation

- Full Agent suite: 771 passed, 20 existing platform skips, 103 subtests passed.
- Ruff format/lint, mypy, locked dependency sync, Template checks and generation/check passed.
- Real packaged Claude and Codex dispatcher processes rejected missing lanes and raw overrides before launch.
- A fresh native Codex request using the L4 table model and effort succeeded.
- Native review and parent API/workflow review completed; generated output is published by the existing post-merge job.

## Delivery

Core's handoff guidance and generated agent pins have a companion change. Core activation requires its Renovate-owned Agent runtime dependency update; the protected pin is unchanged here.

The separately authorized machine cleanup removed the handwritten user-wide Claude and Codex instruction files. Those local deletions are not repository changes. Repository plugin selection and global configuration cleanup remain with their existing configuration owner.
