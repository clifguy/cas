# Specialized workflow runtime selection

Select the **actual executing host** before executing a specialized procedure. Read
`project-policy/references/runtime.md` from the complete shared bundle explicitly
selected by the intended worktree's guide, then read only the matching CAS adapter:

- Codex: [Codex mechanics](codex-runtime.md).
- Claude Code: [Claude mechanics](claude-runtime.md).
- Unknown or ambiguous host, missing selected shared selector or adapter: stop the
  dependent operation and name the missing capability. Never translate tool names.

A directory such as `.agents/skills` or `.claude/skills`, package platform label or
model name does not establish the host. Claude may consume a guide-selected `.agents`
bundle. Read the selected entry point directly if native same-name discovery would
choose another installation. Record guide-directed selection separately from automatic
host discovery. Missing siblings block use; never fall back to a personal bundle.

The adapters supply mechanics, while canonical procedures and live CAS authority
supply policy. Existing activation does not authorize replacing its package. A new
candidate remains trial-only until separately authorized replacement and verification.

## Common execution and evidence obligations

- Resolve the selected complete bundle and its exact sibling entry points. A slash command is a workflow, not a callable tool. Missing required dependencies block the affected step.
- Respect the host's actual planning and approval capabilities. Reuse valid existing authorization and preserve narrower caller restrictions. Publication of comments requires explicit user or approved workflow authorization.
- Use current execution tools. Anchor every command to a verified absolute worktree via `workdir` or `git -C`. Shell variables and cwd do not carry across invocations. `git worktree list` is repository-scoped, not global: run it against a known checkout, parse full porcelain records, and verify the selected repository/branch/path. Resolve the actual forge repository, remote and base branch; do not infer them from an arbitrary sibling worktree. Preserve dirty, untracked, ignored and unrelated staged work.
- Long commands require a durable process/session handle and actual terminal completion through the selected host adapter. Keep individual waits at most 60 seconds, retain one deadline, and report meaningful changes. A running command or ended assistant turn is not success. Cancellation stops only the local watcher unless the caller separately authorized a remote cancellation.
- Resolve the selected SAGE capability from the compatible shared project-policy bundle for every SAGE operation; follow its explicit external or compatibility route. Inspect the served tool contract and target vault's live configuration. Names, field shapes, URIs, pagination and commands in the workflow are examples to validate, not substitutes for that contract. Retrieve current project-owned steering policy before applying a gate; preserve its authority and report conflicts or unavailable policy. Never mutate vault storage directly except a specifically authorized test-fixture exception within its exact bounds.
- Paginate all populations used for an exhaustive claim, including PR comments/checks, catalog rows and edges. A failed, truncated, forbidden or malformed lookup is unknown, not zero, absent, green or waived. Resolve version chains through supported chain semantics; document ids are opaque, lifecycle alone does not identify a head, and unrelated chains sharing a ticket id are collisions. Read edge validity/retraction state before treating an edge as a live dependency.
- Use structured arguments or safely quoted local files for multiline text and shell data; use `--body-file` for PR comments/descriptions. Keep secrets and transferred bytes out of tool output. Read back external writes and retain item-level receipts. After an ambiguous write, reconcile before retrying; never duplicate a comment, ticket or transfer by blindly retrying.
- Never edit permission settings to bypass denial. Use actual host escalation and report unresolved automatic-review refusals with their stated reason. Conversation approval does not change sandbox permissions.

## Batch worker lifecycle

This contract applies to pilot, dispatch loop, completion parsing, focused continuation,
recovery and fresh-session fallback. Host adapters realize it with exposed tools only.

1. Dispatch a fresh implementation context without author conversation history, with
   exact absolute repository/worktree scope, ownership, raw task and existing authorization.
   Preserve other workers' edits. Keep **one active writer** per task and serial cohort
   dispatch. Ordinary implementation settings are distinct from independent review settings.
2. Retain the returned durable dispatch handle, requested settings and actually observed
   settings separately. Await terminal completion and record the actual terminal result.
   Running, interrupted or unknown state is incomplete, regardless of elapsed time.
3. Require the procedure's complete `STATUS`, `TICKET`, `BRANCH`, `PR`, `HEAD`, `REVIEW` and `NOTES` block. Missing
   STATUS, wrong candidate identity or unresolved branch/PR is not success. Independently
   verify Git branch/head, forge repository, PR base/head and scope before advancing.
4. Use a focused continuation on the same supported handle for completed-but-incomplete
   work. Reconcile uncertain external outcomes before repeating any action. Never duplicate
   a completed commit, PR, comment, merge or write because a reply was lost.
5. Before takeover verify the original worker **and its execution sessions have stopped**
   writing. A timeout is not stopped-writer evidence. If state is unknown, park the task;
   no second writer. If continuation/status/stop is unsupported, retain evidence and report
   that capability blocked. A fresh-session fallback has the same stopped-writer gate.

## Independent review handoff

Use the selected shared convergence/review workflow and its review-evidence contract.
Carry exact repository/PR, base/head, pass, cycle/ordinal and correlation token. Reviewers
receive raw requirements and candidate scope without author conversation history; authors
own disposition. Requested model, effort and isolation are distinct from observed evidence.
Unsupported required settings block review pending an explicit owner alternative; never
silently downgrade or claim isolation from a token alone. Stale, missing or contradictory
review/disposition cannot satisfy a gate. No adapter expands merge or closure authority.
