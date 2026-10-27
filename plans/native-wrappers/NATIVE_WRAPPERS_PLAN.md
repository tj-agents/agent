# Native Agent wrappers

Owner slice of the approved cross-repository `native-duplicates/migration` plan in the Docs repository. Tommy approved implementation on October 27, 2026 and requires independent native capability checks and separate host adapters/runtime. Sessions stay open throughout.

## Scope and resolved authoring brief

Agent owns session behavior. Extend `agent:message` and `agent:peer` in their existing canonical utility directories for their additional recipient, delivery and registered-terminal requirements. Delete the incorrect Codex memory utility and its replacement `agent:memory`; native memory controls need no house wrapper. Retained shared skills have concrete examples and explicit host entries declared in `.agents/plugins/payloads.json`. Template's ownership check precedes the loaded native creator. No new runtime engine or dependency is needed.

```text
agent:message -> Claude ListAgents/SendMessage; Codex codex queue
Memory settings -> use the active host's native controls directly; no house memory skill
```

Message authorization, exact recipient selection and accurate status are shared. Native queue admission is `queued`, not proof of delivery. The installed Codex 0.163.0-alpha.1 loopback fixture observed only `thread/queue/add`; its success output lacks recipient-history correlation. Preserve the existing receipt/history and active-turn helper only for explicitly required verified delivery or active correction. Move it from shared skill siblings to `.codex/runtime/codex_followup.py`, packaged under `.agents/agent/runtime/codex/`. Only the Codex adapter references it. Preserve authorization, scope, idempotent receipts and nearest-package imports. Claude uses native tools exclusively.

Peer native listing and inspection are first choice for the current host. Retain existing deterministic cross-host registration and exact visible terminal identity operations where those are requested. The adapter must distinguish native session identities from physical terminal identities.

Delete legacy peer `close.py` and `finish_reaper.py` after source caller proof; remove their dead `start_observer` wrapper. Keep live `session_close`, detached launch and client watcher. Redirect its staging test to the underlying live detached primitive. This task never invokes a close operation.

D2 shell duplicate removal is already on this branch's base through Agent #24. D4 terminal consolidation remains conditional on the CLI API/caller investigation. Preserve Agent #22 permission changes and #20 publication hints.

## Native references and boundaries

- Claude memory: `https://code.claude.com/docs/en/memory`, including native `/memory`, scope and restricted background-session behavior.
- Codex memory: `https://developers.openai.com/codex/config-reference/`, `features.memories`; installed `codex features --help` confirms native list/enable/disable. Never write `.codex/settings.local.json` or assume the feature is enabled.
- Messaging and peer evidence: central inventory N1/N2 plus isolated queue fixture in `%TEMP%/codex-message-queue-fixture-20261027.py`. No real message was sent.
- Existing package manifest already separates `.claude/runtime` and `.codex/runtime`; use its established adapter/resource model.

## Standards and validation

Apply Agent AGENTS.md, Core CODE_CONVENTIONS.md and PACKAGING.md, engineering:docs/skill-authoring and Template authoring metadata. Add no code comments. Edit authored sources, regenerate, and run generated `-Check`, the complete Agent unittest suite, targeted follow-up/loader/staging tests, paired instruction checks and diff checks. Add a packaging regression only for the meaningful boundary that the Claude skill has no executable Codex follow-up sibling/reference and the Codex adapter resolves the packaged helper. Native help and deterministic fixtures validate transports; they do not prove live recipient reachability.

Commit completed verified changes with Codex attribution, review the frozen candidate and publish one Agent PR with Why/What. Completion of this slice does not complete the cross-repository goal.

```agent-host-coverage
{
  "schema_version": 1,
  "shared_source": ".agents/plugins/payloads.json",
  "hosts": [
    {
      "host": "claude",
      "behavior": "native message and peer operations with additional delivery and terminal requirements; no memory wrapper",
      "source": ".agents/plugins/adapters/claude/agent/message.md",
      "mapping": "Canonical Agent skills select explicit host adapters; Codex helper is authored in .codex/runtime.",
      "verification": {
        "level": "source",
        "result": "passed",
        "evidence": "Generated native host boundary and loader/follow-up/staging targeted tests passed; installed acceptance remains pending."
      }
    },
    {
      "host": "codex",
      "behavior": "native message and peer operations with additional delivery and terminal requirements; no memory wrapper",
      "source": ".agents/plugins/adapters/codex/agent/message.md",
      "mapping": "Canonical Agent skills select explicit host adapters; Codex helper is authored in .codex/runtime.",
      "verification": {
        "level": "source",
        "result": "passed",
        "evidence": "Generated native host boundary and loader/follow-up/staging targeted tests passed; installed acceptance remains pending."
      }
    }
  ]
}
```
