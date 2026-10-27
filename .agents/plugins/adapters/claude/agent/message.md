## Claude messaging

Discover the native `ListAgents` and `SendMessage` tools and read their exposed schemas. Select the exact
recipient from `ListAgents`, preserving its native handle and available checkout/profile evidence, then
send once through `SendMessage`. Use the native child-agent route when the recipient is a child of this
conversation; use the independent-session route only when the exposed tool supports that recipient.
Do not invent a recipient handle or transplant one from another host.

Retain the returned native identifier and report the tool's stated submission or delivery status.
Acknowledgment requires an actual recipient response. When native discovery cannot resolve an eligible
recipient or the tool is unavailable, follow the canonical unavailable path; do not invoke a Codex helper,
edit transcripts or inject terminal keystrokes.
