# Work item session store

Central local record of every native Claude/Codex session and work-item status, written by hooks.
Location: `~/.agents-state/sessions.db` (honouring `AGENT_STATE_DIRECTORY`), SQLite in WAL mode. No
`ACTIVE_SESSIONS.md` and no `Status:` field in work-item READMEs; status lives in the database.

## Tables

- `sessions`: one row per `<host>:<native-session-id>` handle. Columns: host,
  session_id, machine, profile, title, cwd, repo, branch, head, work_item, role (`owner` or
  `bounded: <scope>`), pid, pid_started_at, started_at, last_activity_at, turn_state
  (`working`/`awaiting_input`), ended_at, end_reason (`finished`/`closed`/`handed_off`), predecessor,
  successor.
- `work_items`: one row per local `<repo>:<feature>/<item>` key or `execution:<uuid>` native key. Native
  rows have both `feature` and `item` null. Columns: repo, feature, item, status
  (`proposed`/`active`/`blocked`/`paused`/`delivery-gated`/`done`), branch, pr, durable title,
  repository-relative plan path, updated_at.
- `work_item_references`: native provider references bound to a work item. GitHub references use
  `github.com/<owner>/<repository>` and a positive canonical issue number. Azure DevOps references use
  `dev.azure.com/<organization>` and a positive canonical work-item ID.
- `repo` is the `origin` remote as lowercase host plus path (`github.com/owner/name`; Azure DevOps SSH, HTTPS and legacy `visualstudio.com` remotes map to `dev.azure.com/<org>/<project>/_git/<repo>`, including the short `<org>.visualstudio.com/[DefaultCollection/]_git/<repo>` form, which maps to `dev.azure.com/<org>/<repo>/_git/<repo>`), else the absolute `git-common-dir`.

## Hook lifecycle

SessionStart (called in-process from `register_session.py`, after its existing registry write) upserts
the row and clears `ended_at`/`end_reason`, because a resumed session is alive again. UserPromptSubmit
sets `turn_state: working`. Stop sets `turn_state: awaiting_input` and refreshes cwd/branch/head. Claude
SessionEnd sets `ended_at` and keeps an earlier end reason, else `closed`. Codex has no SessionEnd hook,
so a hand-closed Codex tab reads as interrupted. Start and Stop attach an unclaimed session to a work item
when exactly one non-`done` work item in the same repo records the current branch. `close.py`/`machine:close`
best-effort record `closed` for their own session before closing it; their exit observer
deletes the row once the host has exited. Marking a work item `done` deletes its other ended
sessions, and those on this machine whose process has exited; live ones, running ones on other machines and
the caller's stay until their own closeout.

## Read-time status

Computed, never trusted from storage, in order: `foreign` when the recorded machine differs; for a
not-ended row, `live`/`unknown` from `agent_recovery.liveness`; an ended row reports `finished`/
`handed_off` directly; otherwise (ended `closed`, or not-ended and dead), worktree staleness
(`stale`/`unknown` when the worktree is missing, its branch differs, or its recorded HEAD is not an
ancestor of the worktree's current HEAD), else `closed`. Only `closed` authorizes a resume.

## Commands

`recover-agents/scripts/work_item_sessions.py` is the shipped CLI facade over the store. Run it with
`python -B` on Windows or `python3 -B` on Linux/macOS:

```sh
python -B '<skill-directory>/scripts/work_item_sessions.py' status '<feature>/<item>'
python -B '<skill-directory>/scripts/work_item_sessions.py' resume '<feature>/<item>'
python -B '<skill-directory>/scripts/work_item_sessions.py' item '<feature>/<item>' --status proposed --title 'Useful task title'
python -B '<skill-directory>/scripts/work_item_sessions.py' claim '<feature>/<item>' --host codex --role owner --status active --title 'Useful task title'
python -B '<skill-directory>/scripts/work_item_sessions.py' register --provider github --scope github.com/owner/repository --native-id 42
```

- `claim [<feature>/<item>|--execution-key execution:<uuid>|--provider <provider> --scope <scope> --native-id <id>]
  --host claude|codex [--role owner|"bounded: <scope>"] [--predecessor <handle>]
  [--status <status>] [--title <title>] [--plan-path <path>]`: requires the current host's exact native
  session identity. A local selector creates a new item as `active` when `--status` is omitted; a native
  reference must first be registered. For an existing item, omitted status and metadata preserve their
  stored values. New local items require a non-empty `--title`.
  `--plan-path` must be an existing file within the current repository, written relative to its root with
  `/`; `GOAL.md` paths are valid. A successful `owner` claim fills a still-`NULL` branch, in the
  same transaction, when the current branch is not the default. Refuses a second live owner unless
  `--predecessor` names the live owner's handle, which hands its ownership off. Claiming `owner` runs one
  transaction that also clears `role` on every other owner row for the key, so at most one row owns a
  work item at a time, ever; a partial unique index enforces this at the database level. `--role` must be
  exactly `owner` or `bounded: <non-empty scope>`.
- `status [<feature>/<item>|--execution-key execution:<uuid>|--provider <provider> --scope <scope> --native-id <id>]`:
  read-only; reports the durable title and plan path alongside each session's
  role, turn state and computed status.
- `find --worktree <absolute-path>`: read-only; reports the work item, execution key, native references and handle
  recorded for that path.
- `resume [<feature>/<item>|--execution-key execution:<uuid>|--provider <provider> --scope <scope> --native-id <id>]
  [--handle <handle>]`: without `--handle`, resumes the single owner that is not
  ended or ended `closed`; refuses foreign, stale, live, unknown, finished or handed-off sessions, then
  calls `agent_recovery.open_session`.
- `interrupted [--resume <handle>...]`: lists not-ended sessions whose recorded pid is dead, grouped by
  work item with their last turn state and computed status; resumes only listed handles whose status is
  `closed`, refusing stale, unknown, foreign, live, finished or handed-off ones.
- `end --host claude|codex --reason finished|closed|handed_off [--successor <handle>]`: records the
  current session's end reason.
- `item [<feature>/<item>|--execution-key execution:<uuid>|--provider <provider> --scope <scope> --native-id <id>]
  --status <status> [--pr <url>] [--title <title>] [--plan-path <path>]`: records
  work-item status, filling a still-`NULL` branch from a non-default branch. Creating a new item requires
  a non-empty title; no plan path is required for a quick chore or an item whose plan is not yet known.
- `register --provider <provider> --scope <scope> --native-id <id>`: atomically creates or finds one native-only
  execution and binds the reference without claiming a session. It does not use cloud credentials or network access.
- `bind [<feature>/<item>|--execution-key execution:<uuid>] --provider <provider> --scope <scope> --native-id <id>`:
  atomically binds an existing execution. Repeating the same binding succeeds unchanged; a reference already bound to
  another execution is refused. Binding does not change sessions, owners, status or branch.
