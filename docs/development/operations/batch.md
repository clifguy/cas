# /batch

**INACTIVE proposal.** This staged procedure does not govern current work or authorize execution.
Resolve current project policy and caller authorization before adoption or use.
Read [Codex runtime](references/codex-runtime.md) and [dependencies](references/dependencies.md).

Operational skill for cohort planning + execution: take a set of templated CAS tickets, resolve every recurring design question once at the cohort level, persist the decisions as a vault-resident reference document, validate the end-to-end mechanism with a single-ticket pilot, pause at a structural dispatch gate, fire per-ticket subagents that each land their work as a draft PR, pause at a structural merge gate, then land every PR on main via `/merge`. End-to-end in a single session.

The skill exists because per-ticket `/kickoff` ceremony scales poorly across templated cohorts — each `/kickoff` re-asks the same design questions — and because unattended batch execution has subtle failure modes that must be defended against up-front, not discovered mid-run.

## Landing model (v2.0.0)

Historical landing terminology below does not authorize cleanup. Apply the current
compatible merge contract: separately authorized safe cleanup only; report pending
cleanup independently of durable landing. A prior exact cohort grant is reusable,
but the DISPATCH and MERGE structural boundaries must each have been reached and
explicitly approved. General plan approval does not cross either boundary.

v2.0.0 aligns `/batch` with the PR-based landing model that `/kickoff` (v0.2.0+), `/commit` (v0.2.x), and `/merge` (v0.2.x) now share. The change is structural, not cosmetic:

- **`/kickoff` owns provisioning.** Each subagent's `/kickoff {ticket_id}` provisions a named worktree on branch `codex/<descriptive-slug>`, forked from `origin/main`.
- **`/commit` pushes and opens a draft PR.** The subagent's `/commit` commits, pushes the branch, and opens a draft PR. It no longer withholds the push.
- **`/merge` lands the PR.** Landing is a GitHub squash-merge driven by `/merge` (mark ready → squash auto-merge, which enqueues where a merge queue is configured → wait → separately authorized safe cleanup). Since `/merge` v0.8.0 it neither rebases nor pushes. The dispatcher no longer cherry-picks worktree branches onto a local `main`.

What this deletes from the old (v1.0.0) `/batch`: the entire local-landing engine — the fast-forward probe, the cherry-pick path, local worktree teardown, and the dispatch-loop branch-deletion logic. All of that now lives in `/commit` and `/merge`. `/batch`'s remaining job is its real value: **cohort decision-sheet planning, the disjoint-edit-window invariant, two structural approval gates, and orchestrating the per-ticket `/merge` landings.** See §16 for the genealogy and the historical provenance reference for superseded local-landing notes.

This skill defers to:
- *CAS Ticket Conventions* steering document for ticket-read and ticket-close mechanics.
- *ticket-management* skill for any per-ticket write workflows.
- *kickoff* skill for per-ticket worktree provisioning and test-first planning inside each subagent.
- *commit* skill for the per-ticket commit→push→draft-PR workflow inside each subagent.
- *merge* skill for the per-ticket PR landing driven from the dispatcher's merge loop.
- The governing methodology of whichever cohort is being planned.

All operations target `vault_id="cas"`.

**`/close-ticket` is human-gated and is NEVER invoked by subagents or by the skill itself.** Closing a SAGE ticket compresses multiple gates the user keeps separate (commit landing, review, additional manual tests, the explicit "this is done" decision). A subagent or skill running `/close-ticket` at the tail of an autonomous run collapses gates the user has explicitly rejected collapsing (see the live *CAS Invocation Authority* steering document). The skill reports each landed ticket as `ready_for_close` in the final report; the user runs `/close-ticket <ticket-id>` per ticket when satisfied. Auto-commit and auto-merge may be authorized per cohort (via the two structural gates); auto-close is not authorizable.

---

## §1 When to invoke

Three qualification criteria — all must hold:

1. **Templated against a methodology.** The cohort traces to a steering or reference document that prescribes the work shape (e.g., "install a closure pair for each finding"). If the tickets are heterogeneous design exercises, defer to per-ticket `/kickoff`.

2. **Cohort tag cluster.** Every ticket carries a shared identifying tag — usually `follow-up-<ticket-id>` per the *CAS Ticket Conventions* emission rule. Without a cohort tag, membership is implicit and error-prone.

3. **≥3 tickets.** Below that threshold, the ceremony of authoring a decision sheet exceeds the savings from amortizing per-ticket planning.

Two early signals that a cohort is a poor fit even when the criteria hold:

- **Pre-existing per-ticket plans that diverge significantly.** If multiple tickets in the cohort already have substantive `/kickoff` artifacts that took different shapes, the cohort isn't actually templated — defer.
- **Heavy upstream dependencies across cohort members.** If ticket A blocks ticket B blocks ticket C transitively, the work isn't a cohort, it's a sequence — `/kickoff` each in dependency order.

---

## §2 Phase 1 — Cohort discovery

Goal: build a complete picture of the design surface before asking the user anything.

### §2.1 Read the governing methodology

```
search(vault_id="cas", mode="catalog",
              filters={"doc_type": "steering_document",
                       "tags": [<methodology-tag>]})
```

Then `get_document` on the head row. Read the full body. Identify:

- The two-question test / closure criteria / acceptance template the methodology imposes.
- Any inaugural template the methodology cites (e.g., <ticket-id> for projection points). Read that ticket's body too — it carries the canonical implementation shape every cohort member will copy.
- Any source-shape exclusions or carve-outs that change how a subset of cohort members must be handled (e.g., BH-101 in the projection-point methodology).

### §2.2 Read the inventory (if one exists)

Most methodologies that produce cohorts also produce an inventory document. If present, it lists every cohort member with disposition and follow-up ticket mapping. This is the canonical cohort roster — use it instead of inferring membership from tags.

```
search(vault_id="cas", mode="catalog",
              filters={"doc_type": "reference_document",
                       "tags": [<methodology-tag>, "inventory"]})
```

### §2.3 Read every ticket body in the cohort

```
search(vault_id="cas", mode="catalog",
              filters={"doc_type": "ticket",
                       "tags": ["follow-up-<ticket-id>"],
                       "lifecycle_status": "active"},
              limit=50)
```

Then `get_document` on each — in parallel where possible. **Read every body, do not sample.** The decision sheet's promise of "skill runs unattended without re-asking" depends on every per-ticket variation being catalogued at planning time.

### §2.4 Categorize design questions

Working from the methodology, inventory, and ticket bodies, build three lists:

| Category | Definition | Treatment |
|---|---|---|
| **Settled by methodology** | The steering doc prescribes the answer unambiguously. | Record for traceability in the decision sheet's *Cohort-wide defaults*. No user input. |
| **Settled per-ticket** | Each ticket's body prescribes the answer for that ticket (specific factory name, file:line, field set). | No cohort-level decision. Each subagent reads its ticket and applies. |
| **Genuinely open** | The methodology and ticket bodies offer alternatives but don't pick. | Surface to the user in Phase 2. |

The genuinely-open list is what Phase 2 asks about. **Aim for ≤4 items.** If more, the cohort may be too heterogeneous; revisit §1 before continuing.

---

## §3 Phase 2 — User decisions

Ask the user the genuinely-open questions via `the available user-input tool (or a concise conversation question)`, one tool call, ≤4 items.

Each question must:

- Name the affected tickets explicitly in the question stem (e.g., "applies to <ticket-id>, <ticket-id>, <ticket-id>, <ticket-id>, <ticket-id>").
- Recommend a default — first option, labelled "(Recommended)".
- Explain the tradeoff in the option description, not the question stem.
- Stay focused on one decision per question.

If the methodology has an obvious default and the deviation cost is low, **do not ask** — apply the default and record it as a cohort-wide default in §4. Asking unnecessary questions burns user attention and dilutes the questions that matter.

---

## §4 Phase 3 — Decision sheet authoring

The decision sheet is the cohort's working artifact. It must be self-contained enough that a fresh subagent (no conversation memory) can read it once and proceed.

### §4.1 Required sections

1. **Front matter** — title (with cohort identifier and originating ticket), status, date, authority (defers to methodology on conflict), scope (this cohort; potentially reusable for similar future cohorts).

2. **Headline decisions** — one short paragraph per Phase-2 answer, with rationale. These are the user's explicit calls.

3. **Cohort-wide defaults** — the skill's own picks for questions not asked of the user (sentinel fixtures, test naming convention, docstring boilerplate, commit granularity, out-of-scope reaffirmation).

4. **Per-ticket parameter table** — one row per ticket. Columns typically include: ticket id, target model/component, source, whether a new factory is introduced, factory location, test file, test function name. Make this table copy-pastable into the subagent's working context.

5. **Conflict map** — list of source files touched by ≥2 cohort tickets, with the ticket ids. Used for sequencing and for the disjoint-edit-window invariant check (§4.3).

6. **Dispatch order** — numbered list, with one-sentence sequencing rationale (foundation first, dependencies respected, fan-out last, file-overlap minimized in adjacent positions). The same order governs the merge loop (§10): foundation PRs merge before the consumer PRs that rebase onto them.

7. **Templates** — code scaffolding the agent fills in: docstring boilerplate, test scaffolding, factory shape. Provide as literal code blocks with `<placeholder>` markers.

8. **Reuse note** — what's cohort-specific vs. portfolio-generalizable. Promote a decision into the steering document only after a second cohort confirms it generalizes.

9. **Cross-references** — links to methodology, inventory, inaugural template ticket, originating cohort ticket.

10. **Review authorization and ownership** — record the grant for independent review/disposition publication and routine in-scope repairs, separately from commit and merge grants. Name the authoring worker as disposition owner, the current convergence policy and its stopping boundary. Missing publication/repair authority parks the affected stage; it is not inferred from Auto-commit or a merge grant.

### §4.2 What the decision sheet does *not* include

- Per-ticket implementation steps. Those live in the ticket body.
- Test fixture code. Per-ticket; sentinel patterns vary.

### §4.3 The disjoint-edit-window invariant (the primary conflict-prevention guarantee)

For single-file cohorts (and more generally, cohorts where multiple tickets touch the same file), the planner's job is to verify that **each ticket's edit window does not overlap any other ticket's edit window.** When this invariant holds, each PR rebases cleanly against an `origin/main` that already carries the earlier cohort PRs, and §11's conflict-resolution machinery doesn't fire — that is the success mode for batched dispatch.

**Under the v2.0.0 PR-landing model this invariant is load-bearing, not merely the happy path.** `/merge` refuses automated conflict resolution by design: it halts, and since v0.8.0 it does not even attempt an update. So the disjoint-edit-window invariant is the cohort's *primary* conflict-prevention guarantee; §11's authorized mechanical resolution is the recovery net behind it, not a substitute for it. A violated invariant under v1.0.0 produced a recoverable local cherry-pick conflict; today it produces a `CONFLICT` halt that the dispatcher must resolve in the worktree before the PR can land. Get the invariant right at planning time.

When two tickets do touch the same line-window anchor, the planner has three options:

- **First option: split the ticket scopes** so they target disjoint windows.
- **Second option: introduce a foundation ticket (§4.4)** that owns the shared region.
- **Falsification test:** if neither is possible, the §1 "≥3 tickets" qualifier should be revisited. The cohort may be a sequence in disguise — defer to per-ticket `/kickoff` in dependency order.

State the invariant explicitly in the decision sheet's §5 conflict map: "Every ticket's edit window is disjoint from every other ticket's edit window. Each PR is expected to rebase against main without conflict." If this cannot be stated honestly, the cohort isn't ready to batch.

### §4.4 Foundation-ticket pattern

A *foundation ticket* establishes shared content (a module-level block, a shared fixture, a reusable type) that downstream tickets in the cohort consume via cross-reference. The pattern appears when the cohort needs DRY treatment of a section that would otherwise be duplicated across multiple per-tool sites.

Required scope-split discipline:

- **Foundation ticket touches ONLY the shared site.** It does NOT modify any per-tool site, even when the per-tool sites will reference the shared content.
- **Consumer tickets touch ONLY their per-tool site.** They do NOT modify the shared content, and their text assumes the shared site exists above (e.g., "see the family-shared block at top of file for X").
- **Dispatch order is foundation-first.** The foundation ticket is the pilot or the first dispatched ticket; in the merge loop it is the first PR landed — no consumer PR may merge before the foundation PR is on main, so consumer rebases pick up the foundation content cleanly.

State the scope split explicitly in both ticket bodies and in the decision sheet's per-ticket parameter table footnote. Without it, overlapping diffs from independent subagent worktrees will conflict when the second PR rebases.

**Worked example (<ticket-id> / <ticket-id>, 2026-05-25 sage admin docstring cohort).** <ticket-id> added a module-level family-shared block; <ticket-id>, <ticket-id>, <ticket-id> each added per-tool docstring rows that cross-reference the family-shared block. <ticket-id>'s decision-sheet row was "edits ONLY the top-of-file family-shared block; does NOT modify the three admin tools' docstrings." <ticket-id>'s rows were "omits family-shared rows; adds cross-reference comment assuming the block exists above." <ticket-id> was the pilot; <ticket-id> followed in dispatch order. (That cohort predates v2.0.0 and landed via local cherry-pick; under v2.0.0 the identical scope split produces clean rebases instead — see the historical provenance reference.)

---

## §5 Phase 4 — Persist to vault

### §5.1 Stage outside the vault

`ingest_document` reads from the staging path and copies into the vault. Stage to `/tmp/` (or another path outside the vault):

```
/tmp/<cohort-slug>-canonical-decisions.md
```

The served ingest contract selects the retained-source destination. If it returns an upload recipe, resolve the shared transfer capability, execute and verify that recipe, and reconcile terminal ingest state before continuing. **Do not stage directly into the vault's `imports/` directory** — that's the destination after ingest, not the staging location.

### §5.2 Ingest

```
ingest_document(
    vault_id="cas",
    source="/tmp/<cohort-slug>-canonical-decisions.md",
    source_type="markdown",
    metadata={
        "title": "<Cohort identifier> — Canonical Decisions (<originating-ticket> Follow-up Set)",
        "project": "CAS",
        "doc_type": "reference_document",
        "document_date": "<today ISO>",
        "tags": ["reference", "<cohort-topic>", "decisions", "cohort",
                 "<methodology-tag>", "phase-N", "<originating-ticket>"],
    },
    needs_review=False,
)
```

Capture the returned `document_id`. The decision sheet's id is referenced from every subagent prompt — keep it handy.

### §5.3 Wire edges

Mandatory: `references` edge from decision sheet → methodology steering doc.

```
create_edges(
    vault_id="cas",
    items=[
        {
            "source_id": "<decision-sheet id>",
            "target_id": "<steering-doc id>",
            "edge_type": "references",
            "source_valid_from_version": "<verified decision-sheet version>",
            "target_valid_from_version": "<verified steering-document version>",
            "rationale": "Decision sheet operationalizes the <methodology> for the <cohort> cohort; this edge anchors the sheet to its governing steering document.",
        }
    ],
)
```

Optional: per-ticket `references` edges from the decision sheet to each cohort ticket. Tradeoff is discoverability versus edge noise. Default: do **not** wire per-ticket edges. Ask the user if they want per-ticket wiring.

---

## §6 Phase 5 — Pilot validation *(critical — do not skip)*

Dispatching a full cohort blind is the most expensive mistake in batch planning. The first ticket is the test — and under the v2.0.0 PR model the pilot validates the **entire** chain end-to-end: `/kickoff` provisions a named worktree → `/commit` pushes and opens a draft PR → the authoring worker establishes applicable independent review/disposition → `/merge` squash-merges it onto main.

### §6.1 Dispatch one ticket as a single subagent

Before pilot dispatch, record the verified primary checkout path, local HEAD and complete status, plus the freshly fetched remote default ref/tip, as separate ledger fields. Require the existing clean-checkout prerequisite; preserve that local baseline throughout the cohort.

Construct the per-ticket subagent prompt from the §8.2 template with the first dispatch-order ticket id substituted. Dispatch one fresh `collaboration.spawn_agent` with `fork_turns="none"`, passing the rendered prompt as `message`. Record its returned agent id and await its completion before another dispatch. Use the session model/effort by default; this is an implementation worker, not an independent review pass.

### §6.2 Verify after the subagent returns

1. **Subagent succeeded?** The STATUS line should be `pr_open` (the subagent committed, pushed, and opened a draft PR). `blocked` → halt per §12.
2. **Branch, PR and review evidence resolve?** The subagent reports `BRANCH`, `PR`, `HEAD` and `REVIEW`. Confirm the exact repository/base/head, open draft state and current review evidence independently from Git/forge. Apply the review handoff below before any pilot landing. Missing or stale evidence returns to the recorded authoring worker; it does not pass to `/merge`.
3. **Land the pilot via `/merge`.** First verify the caller explicitly authorized this named pilot merge; missing pilot permission parks it before landing. Invoke `/merge <BRANCH>` (thread the subagent-**reported** branch, not a reconstructed `codex/<descriptive-slug>` — see §6.3). `/merge` marks ready, enables squash auto-merge, waits, and reports any separately authorized cleanup. Consume the compatible merge capability's actual result: independently verify the exact PR is `MERGED`, read its merge SHA and fetch the actual default-branch ref. Verify the merge belongs to that ref before recording the pilot's `PR` and `MERGE_SHA`; batch then assigns its own `ready_for_close` ledger state. Do not require a legacy output spelling from the shared skill. Record the remote default tip separately from the unchanged primary checkout.

If `/merge` halts on a conflict, a CI failure, a dequeue, or a timeout during the pilot, treat it as a §12 halt — the pilot exists to surface exactly these before fanning out.

### §6.3 The branch-name linchpin (verify empirically; do not assume harness behavior)

The entire PR chain keys off the subagent's branch name carrying through to `/merge`. `/kickoff` provisions a named worktree on `codex/<descriptive-slug>`; `/commit` opens the PR on whatever branch is checked out; `/merge` resolves the PR by `gh pr view "$feature_branch"`. If the branch name is wrong, the chain breaks silently.

Codex subagents share the filesystem and do not automatically provision a worktree. Give each worker the exact verified repository path and task scope; require `/kickoff` to provision/validate the task's own worktree before edits. Default new branch names to `codex/<descriptive-slug>`. Keep ticket identifiers in SAGE/PR/external evidence, never repository-committed paths or contents. Existing authorized task worktrees may be reused only after checking ownership, branch and state.

Key `/merge` to the worker's actual branch and PR after independently verifying them against Git and the forge. Keep the agent id, worktree, branch, head SHA and PR in the dispatch ledger. A discrepancy blocks that ticket; do not trust one self-reported string over conflicting tool evidence. Do not inspect or change Claude-style isolation settings.



## §7 The DISPATCH GATE (first structural turn boundary)

After the pilot lands on main, **stop**. Do not proceed to dispatching the remaining queue. The user must explicitly authorize fanout.

Emit this gate block verbatim (substitute the bracketed values):

```
═══ DISPATCH GATE ═══
Cohort:         <cohort identifier>
Pilot landed:   PR #<N> squash-merged at <merge_sha> on main
Remaining:      <N> tickets queued for dispatch: <comma-separated ticket ids>
Decision sheet: <vault document_id or title>

Awaiting ;;yp to begin dispatch. Any other reply parks the cohort.
═══════════════════
```

This is a hard structural boundary. The gate's job is to defeat the "blow through plan into dispatch" failure mode — a skill-body instruction can be ignored; a turn boundary cannot.

If the user replies `;;yp` (or unambiguous equivalent: "yes", "proceed", "go ahead"), proceed to §8.

Any other reply parks the cohort. The user may resume later in the same session by saying "resume the cohort" or equivalent — the in-session context still holds the queue, the decision-sheet id, and the pilot ledger entry, so resumption is conversational (§15.1).

---

## §8 The dispatch loop (fan-out → draft PRs)

Process the post-pilot queue in dispatch order, one ticket at a time, foreground only. Each subagent runs end-to-end through `/commit` and stops with a **draft PR open** — it does NOT merge. Landing happens later, in the merge loop (§10), behind the second gate (§9).

### §8.1 Dispatcher state contract

Hold only:

- **The queue.** Ordered list of remaining ticket ids (post-pilot).
- **The running ledger.** For each processed ticket: `(ticket_id, status, feature_branch, pr_number, merge_sha, notes)`. `merge_sha` is null until the merge loop lands the PR. Used for the §13 final report. The pilot's row is the first entry — populate it (with its `merge_sha`) from the §6 pilot landing before firing the first post-pilot subagent.
- **The conflict-resolution policy state.** Default: `halt-and-surface`. Upgraded only after explicit authorization per §11.
- **The known-flaky-check list.** Check names tolerated for a single §10.3 bounded rerun on flaky evidence. Empty by default; seed from the decision sheet or prior-cohort observations. Distinct from the conflict-resolution policy state.
- **Cohort identifier and decision-sheet id** — for the per-ticket subagent prompt and the final report's header.

Do NOT hold:

- The methodology body, the decision sheet body, or any ticket body. Each subagent reads what it needs.
- Implementation details. They live in commit messages, PR bodies, and subagent NOTES.
- Per-ticket plans. The subagent invokes `/kickoff {ticket_id}` and follows that.

### §8.2 Per-ticket subagent prompt template

The dispatch loop instantiates this template per ticket — it is NOT serialized to disk; it lives in the skill body and is rendered in-flight by substituting the bracketed values.

```
You are implementing one ticket from the <cohort identifier> cohort:
{ticket_id}. <One-line work-class description.> Run end-to-end and report
back with a structured status line.

Before starting, read these two artifacts from the cas SAGE vault:

1. <Methodology steering doc title> — doc_type=steering_document,
   document_id `<id>`. The methodology these tickets operationalize.

2. <Decision sheet title> — doc_type=reference_document, document_id
   `<id>`. This sheet pre-resolves every design decision recurring
   across the cohort. Trust it. Follow it.

Then read your ticket via search + get_document with
tier3 filter on ticket_id={ticket_id}.

Workflow:
1. Invoke /kickoff {ticket_id}. It provisions a named worktree on branch
   codex/<descriptive-slug> forked from the verified remote/default branch. Follow the test-first
   sequencing.
2. Implement. Then run your affected tests in the foreground —
   `.venv/bin/pytest <test_file> tests/test_public_posture.py` (add
   `::<node>` to narrow your own file further). Your affected set is
   ALWAYS your ticket's own test file(s) PLUS
   `tests/test_public_posture.py`: every cohort touches durable code
   surfaces, and that test is the authoritative public-posture gate
   (hyphenated `<ticket-id>` in `#` comments/docstrings, use-case terms
   outside `domains/`, personal paths, SDLC-scaffolding terms). It is
   one fast file — preserve this gate when required by the current project policy. Run any additional mandatory local gates; do not treat this template as permission to bypass them. Use the configured interpreter/dependencies for the verified worktree. Long commands may return a session id: poll it and await the terminal exit status before continuing. Never infer success from dispatch or expect a completion notification after ending the task.

3. If the decision sheet authorizes Auto-commit, invoke /commit. Otherwise return prepared-but-blocked before any commit, push or PR creation. /commit runs the pre-commit implementation review (not a substitute for independent PR review), commits, pushes your
   branch, and opens a draft PR. This is expected — do NOT suppress
   the push or the PR.
4. Establish the initial independent-review handoff before returning pr_open.
   Assess current review triggers against the cumulative PR diff and record the
   exact base/head. If a current valid waiver applies, retain its authority and
   scope; if no trigger applies, record the source-backed no-trigger assessment.
   Otherwise invoke the compatible /converge-review workflow with the decision
   sheet's scoped review/publication/repair grant. It creates a fresh independent
   reviewer; you remain the authoring owner of fixes and /disposition. Pre-commit
   review is not that independent pass. Honor current stopping rules and caller-owned
   decisions. Missing authorization, capability, unsettled cycle or stale evidence
   returns blocked with the open PR preserved. Fix commits are permitted by the
   scoped repair grant even though the initial implementation is one commit.
5. STOP. Do NOT invoke /merge. Do NOT invoke /close-ticket. Landing
   the PR on main is the dispatcher's job (it runs /merge behind a
   merge gate); closing the SAGE ticket is human-gated. The
   dispatcher will report your ticket as ready_for_close in the final
   cohort report after it merges your PR, and the user runs
   /close-ticket {ticket_id} when satisfied with the cohort.

Escalate (return STATUS: blocked) rather than proceed when:
- The ticket body conflicts with the decision sheet (sheet governs;
  flag the conflict, do not overrule it).
- Tests fail in a way the ticket scope does not anticipate.
- /commit's code-review gate blocks and the fix is non-trivial.
- /commit cannot push or open the PR (auth/network) — report the
  branch you committed on so the dispatcher can recover.
- Anything else genuinely novel — gaps in the decision sheet are
  themselves worth surfacing.

<Inaugural template ticket id> is the grandfathered closure pair
(<canonical implementation file:line>). Read it once if you need the
canonical template's body alongside the decision sheet's scaffolding.

Auto-commit: <yes | no per cohort policy>. One initial implementation commit when Auto-commit: yes; authorized review repairs may add fix commits.
Only with Auto-commit: yes, push and open the draft PR (that is /commit's job). Do NOT merge. Do
NOT run /close-ticket.

Return a report in exactly this shape (and nothing else after it):

STATUS: pr_open | blocked
TICKET: {ticket_id}
BRANCH: <the branch /commit pushed, or null if blocked before commit>
PR: <PR number or URL if opened, else null>
HEAD: <exact remote PR head SHA, or null>
REVIEW: <settled exact-base/head review/disposition references | source-backed no-trigger assessment | current scoped waiver | blocked reason>
NOTES: <one paragraph — what was done, anything unusual, any
       decision-sheet feedback>
```

### §8.3 Per-iteration procedure

For each ticket in the post-pilot queue:

1. **Pre-iteration sanity.** Preserve the primary checkout at its recorded pre-pilot local HEAD/status; it need not equal the remote pilot merge SHA. Freshly fetch the verified remote default ref and compare it with the expected remote tip recorded after the pilot. Unexpected local changes or remote drift halt per §12; never reset or fast-forward unrelated local work to satisfy this check. Workers provision from the verified remote base through kickoff.

2. **Dispatch one worker.** Use `collaboration.spawn_agent` with a descriptive task name, `fork_turns="none"`, and `message` containing §8.2's prompt plus exact repository/worktree scope and existing authorization. Omit model/effort overrides for implementation. Await this worker's terminal state with current collaboration tools before starting another. A timeout/interruption is not completion; retain the handle and determine whether it is still writing before any takeover.

3. **Parse the subagent return.** Locate the structured block:

   ```
   STATUS: pr_open | blocked
   TICKET: <ticket-id>
   BRANCH: <branch or null>
   PR: <pr number/url or null>
   HEAD: <exact remote PR head SHA or null>
   REVIEW: <verified review state and evidence references>
   NOTES: <one paragraph>
   ```

   Grep for `^STATUS:` at the start of a line. The subagent's self-reported sanity checks above this block are noise; don't parse them.

   Retain the returned agent id and verify `BRANCH`, `PR`, absolute worktree path and head SHA with live Git/forge reads. A mismatch is blocked evidence, never a reason to prefer an unverified self-report.

4. **Branch on STATUS.**

   - **`STATUS: pr_open`** → record `(ticket_id, pr_open, BRANCH, PR, merge_sha=null, NOTES)` in the ledger. Advance to the next ticket. **No landing yet.**
   - **`STATUS: blocked`** → halt per §12. Do not advance.

5. **No local landing, no worktree teardown.** Under the PR model the subagent's worktree holds an open PR awaiting merge; any later cleanup follows the separate grant and safe-ownership checks in §10, not the dispatch loop. Worktrees accumulate across the fan-out — one per ticket — and that is expected. The dispatcher does not unlock, remove, or delete any worktree or branch during fan-out.

### §8.4 Continuous verification (during fan-out)

Between every dispatch iteration:

- The primary checkout retains its recorded local HEAD/status; fetching does not promise checkout movement.
- The freshly fetched remote default tip equals the recorded post-pilot remote tip; unexpected remote advancement is surfaced before another dispatch.
- Each completed ticket has a `pr_open` ledger row with a non-null `BRANCH` and `PR`.

If any of these checks fail mid-cohort, halt per §12.

When the queue is exhausted, every post-pilot ticket has an open draft PR. Proceed to §9.

---

## §9 The MERGE GATE (second structural turn boundary)

After fan-out completes — every post-pilot ticket has an open draft PR — **stop again**. Do not begin merging. The squash-merge is the only irreversible step in the cohort; it stays behind a deliberate authorization, symmetric with the §7 DISPATCH GATE.

Emit this gate block verbatim (substitute the bracketed values):

```
═══ MERGE GATE ═══
Cohort:       <cohort identifier>
On main:      pilot PR #<N> already merged at <merge_sha>
Open PRs:     <M> draft PRs queued to land in dispatch order:
              <ticket_id  →  PR #<n>  (branch)>   ×M
Decision sheet: <vault document_id or title>

Landing plan: /merge each PR in dispatch order; foundation-first.
Each /merge marks ready, enables squash auto-merge, and waits (~CI-time
per PR, serial). Conflicts halt per §11; /merge never updates a branch.

Awaiting ;;yp to begin merging. Any other reply parks the cohort with
all PRs open and unmerged.
═══════════════════
```

This is a hard structural boundary. Nothing reaches canonical main without this deliberate act — the discipline v1.0.0 enforced as a push-gate (§13.4) is preserved here as a merge-gate, with the irreversible squash behind it.

If the user replies `;;yp` (or unambiguous equivalent), proceed to §10.

Any other reply parks the cohort with every PR open and unmerged. The user may merge selectively themselves (`/merge <branch>` per PR) or resume the full loop later in-session (§15.1). Parked PRs are durable — they sit on the remote until merged or closed.

---

## §10 The merge loop (land each PR via /merge)

Process the open PRs in dispatch order, one at a time, **serially**. Serial is load-bearing for foundation-first ordering (§4.4): a consumer PR must be built against a default branch that already holds the foundation. Where a merge queue is configured it builds each entry against the current default branch itself, so a later PR needs no update of its own; serial landing remains the discipline because the *content* dependency is real even when the mechanical one is not.

### §10.1 Per-PR procedure

For each ticket in dispatch order with a `pr_open` ledger row:

The dispatcher verifies the worker's review handoff against the current exact PR
base/head and complete review/disposition records, including the current cycle
state. A required review without a settled disposition blocks landing and returns
to the recorded authoring worker via a focused continuation. That worker coordinates
fresh independent review and owns repairs/disposition; the dispatcher cannot stand
in as either role. Reuse current valid waiver/no-trigger evidence only within its
verified scope. Refresh after each preceding landing or branch change; stale evidence
requires the author to reassess under current policy before merge. This applies to
the pilot as well as ordinary members. Missing author context or review grant is a
blocked handoff, never a waiver.

1. **Pre-iteration sanity.** Confirm the exact PR remains open with its expected head. Freshly fetch the actual remote default ref and require the expected remote tip recorded after the preceding verified landing. Track primary-checkout preservation separately; local HEAD need not advance. Unexpected PR state, remote drift or local changes halt per §12.

2. **Invoke `/merge <BRANCH>`.** Thread the subagent-reported `BRANCH` (§6.3). `/merge` handles everything: PR resolution, the review-protocol preconditions, mark ready, squash auto-merge (which enqueues under a merge queue), the wait (its own silent background loop, reading both the check rollup and the queue entry), and separately authorized safe worktree/branch cleanup. It does not rebase and does not push.

3. **Wait for `/merge`'s terminal state before advancing.** `/merge` reaches one of:
   - **Verified merged result** → independently read exact PR `MERGED` state and merge SHA, fetch the remote default ref, and verify that identity and ancestry. Record `merge_sha`, `pr_number` and remote tip, then assign batch's own `ready_for_close` ledger status. A prose success or legacy status string alone is insufficient. Advance only after these checks.
   - **`CONFLICT` or `BEHIND` halt** → §11 (conflict resolution). The branch needs updating, which `/merge` will not do; the dispatcher does it in the worktree. Do not advance until resolved and the PR lands.
   - **`DEQUEUED` halt** → the queue ejected the entry and did not re-arm auto-merge. Treat as a CI failure: diagnose, fix, re-invoke.
   - **CI `FAILURE`** → apply §10.3 flaky-vs-real discrimination *before* halting. Flaky (same-SHA `SUCCESS` evidence or a known-flaky-list hit) → §10.3's single bounded rerun re-enters the wait. Real (no flaky evidence) → halt per §12; surface the failing check name(s) and URL(s).
   - **`TIMEOUT`** (CI still running at `/merge`'s 10-minute cap) → surface to the user; `/merge <BRANCH>` is idempotent and can be re-invoked to resume the wait. Do not advance to the next PR until this one lands (serial ordering).

   The serial wait is deliberate: the next PR's content depends on this one being on the default branch. Do not fire the next `/merge` until the current PR is `MERGED`.

4. **No dispatcher-side worktree teardown.** Follow the compatible merge capability's current cleanup contract. Cleanup requires its own scoped grant and verified disposable ownership, unchanged merged tip, no active writer and no valuable untracked/ignored content. Preserve dirty or locked worktrees; never force-remove or unlock them. Report durable merge success separately from cleanup pending, and do not delete remote branches.

### §10.2 Continuous verification (during merge loop)

Between every merge iteration:

- The freshly fetched remote default tip contains the exact PR merge SHA. Under the planned serial squash path, the merge parent equals the preceding recorded remote tip and the new tip equals that merge SHA. Unexpected concurrent advancement halts for reconciliation; it is neither local-checkout failure nor automatic merge failure.
- The just-landed ticket's ledger row carries a non-null `merge_sha`.
- Verify the just-merged worktree's actual cleanup state; absence is required only when authorized cleanup was performed. Otherwise preserve it and record cleanup pending without denying a verified merge.

If any of these fail mid-loop, halt per §12.

When the queue is exhausted, every cohort PR is squash-merged on main. Proceed to §13.

### §10.3 CI-flakiness discrimination (bounded rerun)

When `/merge`'s wait loop reports a CI `FAILURE` (§10.1 step 3), do **not** halt unconditionally. `/merge` is categorically strict and flaky-unaware by design — it surfaces the failing check and stops, for any caller. `/batch`, holding the cohort context, applies one bounded flakiness test before deciding the failure is real, then either reruns once or halts. This mirrors the §11 seam exactly: `/merge` stays general and strict; the cohort skill wraps it with regime-specific intelligence, and `/merge` gains no flaky-awareness.

**Flaky evidence — either is sufficient:**

1. **Same-SHA evidence.** A run of the *same* failing check on the *identical* commit SHA concluded `SUCCESS` where this one failed. A check that both passes and fails on one unchanged tree is non-deterministic by definition. Confirm with `gh pr checks <N>` or `gh run list --commit <sha> --json workflowName,conclusion,headSha` and compare conclusions for the same workflow on the same SHA. (Branch-push and PR triggers both fire on the same SHA in this repo's CI, so sibling same-SHA runs are common.)
2. **Known-flaky-list hit.** The failing check matches an entry in the cohort's known-flaky-check list (held in dispatcher state, §8.1; seeded from the decision sheet or prior-cohort observations).

**Bounded rerun — one attempt per PR, ever:**

- If flaky evidence exists: run `gh run rerun --failed` for the failing run **once**, log the decision (check name + which evidence), and re-invoke `/merge <BRANCH>` to resume the wait. The rerun re-enters `/merge`'s idempotent background wait loop.
- If the rerun also fails: halt per §12. A second failure on the same SHA is treated as real (or beyond automated tolerance). Never loop reruns; never rerun silently.

**Real failures (no flaky evidence):**

- No same-SHA `SUCCESS`, and the check is not on the known-flaky list → halt per §12 exactly as today. Surface the failing check name(s) and URL(s). Do not rerun on a hunch.

**Logging.** Every rerun decision and its outcome feeds the §13.3 dispatcher-state report: which check, which evidence triggered the rerun, and whether the rerun then passed. A check that proves flaky within a cohort is a candidate for the decision sheet's known-flaky list (so the next cohort skips straight to the rerun) or for a fix ticket that removes the flakiness at the source.

---

## §11 Rebase-conflict resolution (authorized mechanical policy)

The default policy is **halt-and-surface**. The skill does not auto-resolve rebase conflicts without explicit user authorization. This is the safe default.

When the §4.3 disjoint-edit-window invariant holds, this section should never fire — each PR rebases clean. When it does fire, treat it as a signal that the invariant was overestimated, and feed that observation into the §13 final report.

**Where resolution lives (the v2.0.0 seam).** `/merge` stays categorically strict: it halts on a conflict and never auto-resolves for any caller, and since v0.8.0 it does not update the branch at all. When `/batch` is running under an authorized mechanical policy, **`/batch` — not `/merge` —** rebases in the feature worktree, applies the named rule, verifies, re-pushes, and re-invokes `/merge`. The regime-specific intelligence (the decision sheet, the cross-ticket conflict history) stays in the skill that holds the cohort context; `/merge` needs no change and gains no cohort-awareness. This matches the boundary discipline used elsewhere: the general ship surface stays general; the cohort skill wraps it.

**Single-commit guard (precondition for any mechanical rule).** Before applying a rule, assert the branch under merge carries exactly one commit:

```
git -C "<repo_root>" rev-list --count "<freshly-verified-remote-default-ref>..HEAD"   # must == 1
```

A rebase replays a branch commit-by-commit, so a rule authored against the branch's *final* diff shape can misfire on an intermediate commit. The initial implementation is normally one commit, but authorized review repairs may add commits; compute the count against the freshly fetched actual remote default ref, never a stale local main. If the count is `> 1`, do **not** apply a mechanical rule — fall back to manual halt (§12); the multi-commit-rebase hazard is out of scope.

### §11.1 First conflict: halt and surface

On the first rebase conflict in a cohort (surfaced by a `CONFLICT` or `BEHIND` halt from `/merge`'s wait loop):

1. Inspect: `grep -n '^<<<<<<<\|^=======\|^>>>>>>>' <conflicting-file>` in the feature worktree (`repo_root` keyed to the branch), and show the user the conflict regions.
2. Diagnose each region. Common shapes:
   - **Import additions.** Two tickets each added an import to the same `from X import (...)` block.
   - **Mid-file additions** that one ticket already hoisted to top-of-file (so the other's mid-file addition is now redundant).
   - **End-of-file new sections.** Each ticket appended a new factory+test pair; they happen to land in the same region.
   - **Same-line edits with different intent.** Real semantic conflict — needs human review.
3. Propose a resolution for *this* conflict to the user. State the rule you used, not just the resolution. Surface the patch you'd apply but do not apply it yet.
4. Wait for explicit recovery authorization. Establish the actual rebase state, apply in the verified worktree, verify affected tests, continue only an active rebase, and push with an explicit lease tied to the observed remote head. Re-establish current review/disposition and re-invoke `/merge <BRANCH>`. Continue only after landing is verified.

### §11.2 Recurring conflicts: propose a mechanical policy

When the *same shape* of conflict appears in a second or third PR in the cohort, surface the pattern explicitly:

> Two of the last three PRs hit rebase conflicts of the same shape: [describe shape]. The resolution rule I'd apply is: [state rule precisely]. With your authorization I'll apply this rule mechanically for the rest of the cohort and halt only on conflicts that don't fit it.

The proposal must be:

- **Specific.** Not "I'll resolve conflicts mechanically" — *which conflicts, how*.
- **Bounded.** Lists what the policy *does not* cover (any conflict not matching the rules → halt and re-ask).
- **Verifiable.** Includes a post-resolution check (e.g., "and run the touched test file before `git rebase --continue`").

Example policy from the <ticket-id> cohort (use as template, not as default — every cohort's shape differs; the example predates v2.0.0 and was authored against cherry-pick conflicts, but the rule *shapes* transfer to rebase conflicts):

> 1. Import lines: union both sides; drop duplicates; keep alphabetized within each `from X import (...)` block.
> 2. Mid-file redundant imports that a prior cohort ticket already hoisted to the top: drop them.
> 3. New end-of-file sections (factory + test): keep both, prior-ticket-order first.
> 4. Verify: run `.venv/bin/pytest <touched-test-files>` before `git rebase --continue`.
> 5. Halt on: any conflict whose region doesn't fit rules 1–3.

### §11.3 Applying an authorized policy

When the user authorizes a mechanical policy:

1. **Echo the policy back** to confirm the exact rule set. Once authorized, do not silently extend it.
2. **Confirm the single-commit guard** (top of §11) for the branch under merge before touching anything. `> 1` → manual halt.
3. **For each conflict region, classify** against the rule set. If a region fits a rule, apply. If a region doesn't fit, halt per §12 — do not improvise.
4. **Run the verification step** (typically: pytest on touched test files) before `git rebase --continue`.
5. **Re-push and re-merge.** `git push --force-with-lease=<remote-ref>:<observed-remote-sha>`, then re-invoke `/merge <BRANCH>` to land the PR.
6. **Confirm to the user** after the PR lands: "<ticket-id> landed at `<merge_sha>` via authorized mechanical resolution; conflicts matched [which rules]."

### §11.4 When to retire the policy

If three subsequent PRs land clean (no conflicts), the cohort may have exited the high-overlap zone. The policy is still in force — do not silently retire it — but mention it in the §13 final report as a candidate to extract into the cohort decision sheet or steering doc for future cohorts.

---

## §12 Halt conditions

Halt the cohort (do not dispatch or merge further) when any of these occur:

| Trigger | Surface |
|---|---|
| Subagent returns `STATUS: blocked` | The subagent's NOTES line — pass through verbatim. |
| Subagent reports `pr_open` but `BRANCH`/`PR` is null or does not resolve via `gh pr view` | The mismatch — could indicate a failed push or hallucinated STATUS. |
| Subagent returns control with no parseable `STATUS:` line (yielded mid-task — e.g., it backgrounded a long command and waited for a notification) | Attempt §12.3 stalled-subagent recovery *before* halting — the work may be complete-but-uncommitted; halt only if recovery preconditions fail. |
| `/merge` halts on a conflict whose shape doesn't fit an authorized §11 policy | The conflict regions, with diagnosis of why no rule applies. |
| `/merge` reports a CI `FAILURE` with no flaky evidence (§10.3) — or the single bounded rerun also failed | The failing check name(s) and URL(s). |
| `/merge` reports `TIMEOUT` (and the user does not want to wait/re-invoke) | CI still running; `/merge <BRANCH>` is re-invocable to resume. |
| Single-commit guard fails (count against the freshly verified remote default ref is not one) when a mechanical rule was about to apply | The commit count; fall back to manual resolution. |
| Pre-iteration sanity fails (primary checkout changed, remote default tip drifted, PR unexpectedly closed/merged) | The separate local/remote identities and unexpected state. |
| `/merge`'s worktree teardown fails (e.g., still-locked agent worktree) | `/merge`'s error — a `/merge` concern; surface and let the user decide. |

### §12.1 What halting looks like

1. **Stop the loop.** Do not fire any more subagents and do not start any more `/merge` runs.
2. **Preserve evidence.** Leave open PRs open and feature worktrees intact (do not remove them; the user may want to inspect).
3. **Surface state.** Emit a partial §13 report covering tickets landed so far, plus the halt reason and current state of main and the open-PR set.
4. **Do not speculate.** State what happened, not what to do next. The user decides recovery.

### §12.2 What halting does NOT mean

- Do not invoke `/close-ticket` as part of halt recovery. `/close-ticket` is human-gated regardless of dispatch state; halted tickets stay `active` and the user runs `/close-ticket` per ticket later.
- Do not revert already-merged cohort PRs. They're correct.
- Do not auto-retry the failed dispatch or merge. If the user wants a retry after fixing the underlying issue, they'll say so (see §15).

### §12.3 Stalled-subagent recovery (recover before halting)

A missing status block or elapsed wait does not prove a worker is finished. Inspect the recorded agent handle with the available collaboration status tools. If still running, await it or communicate with it; do not start a second writer. A completed but incomplete worker can receive a focused follow-up through the current collaboration API. If recovery requires takeover, establish that the original worker and its execution sessions have stopped writing first.

Use the dispatch ledger's exact registered worktree, branch and PR; verify against Git/forge state. Preserve existing changes, inspect commit/test evidence, and resume only the remaining authorized work. Do not repeat a commit or PR creation whose response was ambiguous without reconciliation. A fresh recovery worker gets the exact scope and no authoring history from unrelated tickets; it must verify completeness rather than accepting a claim that tests passed. Missing approval or unresolved implementation returns `blocked` with the durable state.

## §13 Final report

On cohort completion (queue exhausted, all PRs merged) or on halt, emit a structured report.

### §13.1 Per-ticket status table

| Ticket | Status | PR | Merge SHA | Landing | Notes |
|---|---|---|---|---|---|
| <ticket-id> | ready_for_close | #<n> | `<sha>` | pilot / merged / mechanical-resolve / pr-open / blocked | <one-line excerpt from subagent NOTES> |

Landing values: `pilot` for the pilot ticket (landed during §6), `merged` for a clean post-gate `/merge`, `mechanical-resolve` for a `/merge` that landed after authorized §11 resolution, `pr-open` for a ticket whose PR was opened but not yet merged (cohort parked at the merge gate or halted before its turn), `blocked` for a subagent that never reached `pr_open`.

Every `ready_for_close` row corresponds to a SAGE ticket still in `lifecycle_status=active`. The skill does NOT run `/close-ticket` on any of them — that gate belongs to the user.

### §13.2 Aggregated portfolio observations

Scan each subagent's NOTES line for portfolio-level signals — patterns or gaps that the next cohort would benefit from knowing. Common shapes:

- **Decision-sheet gaps.** Multiple subagents independently rediscovered the same blind spot or workaround. Candidate for an update to the decision sheet (or, if the gap generalizes, the steering doc).
- **Sentinel/scaffolding repetition.** The same helper construction recurred across N tickets. Candidate for extraction into a shared test helper.
- **Workflow friction.** Recurring pre-commit hook rejections, missing-imports complaints, stale ticket-body references, PR-template gaps. Candidate for `/commit` / `/kickoff` / `/merge` / ticket-template improvements.
- **Decision sheet conflicts surfaced and resolved by subagents on their own.** Worth promoting back into the sheet so the next cohort doesn't re-rediscover.

Group observations by category. Record the concrete observation and affected artifact. Propose follow-up tickets or artifact updates only when the caller requests recommendations; creating a ticket still requires the applicable authorized scope.

### §13.3 Dispatcher-state observations

- PRs opened: total during fan-out.
- PRs merged: how many landed clean vs. via authorized §11 mechanical resolution.
- Rebase conflicts: how many `/merge` runs halted on conflict; were they the same shape?
- Conflict-resolution policy: was one authorized? If so, when, and what was its content?
- §4.3 disjoint-edit-window invariant: did it hold? If not, where did it break (which PR's rebase conflicted, and against what)?
- CI flakiness (§10.3): how many `/merge` CI `FAILURE`s were judged flaky and got a bounded rerun; which checks; what evidence (same-SHA `SUCCESS` vs known-flaky-list); did each rerun then pass? A recurring flaky check is a candidate for the decision sheet's known-flaky list or a source-level fix ticket.

### §13.4 Merge state

Report: "main carries N squash-merged cohort commits (pilot + <N-1> post-gate); M PRs remain open and unmerged." Each subagent's `/commit` already pushed its branch — there is no held-back push to report. The gated action in v2.0.0 is the merge, not the push; if the cohort is parked at the merge gate, say so here.

### §13.5 Close-ticket handoff

Append a one-line handoff after the merge-state line:

> N tickets are ready_for_close. Run `/close-ticket <ticket-id>` per ticket when satisfied with the cohort's code-state.

The skill does NOT invoke `/close-ticket` itself. This is the front-matter rule, reaffirmed at the end of the cohort so the user sees it on the same screen as the final ledger.

### §13.6 Tone

- **Factual.** What happened, what's on main, what PRs remain.
- **Specific.** Merge sha, ticket id, PR number, file paths. Not "everything worked."
- **No recommendations for next steps** unless the user asks.

---

## §14 Discipline checklist

**Planning phase (§§1–6):**
- [ ] Governing methodology steering doc read in full
- [ ] Inventory document read (if one exists)
- [ ] Every cohort ticket body read (not sampled)
- [ ] Inaugural template ticket body read (if methodology cites one)
- [ ] Design questions categorized: settled by methodology / settled per-ticket / genuinely open
- [ ] Genuinely-open list ≤4; if larger, cohort heterogeneity reassessed against §1
- [ ] User questions asked in one `the available user-input tool (or a concise conversation question)` call with recommended defaults
- [ ] Decision sheet includes all §4.1 required sections
- [ ] §4.3 disjoint-edit-window invariant stated explicitly (and either held or addressed via foundation ticket) — it is the primary conflict-prevention guarantee under the PR model
- [ ] §4.4 foundation-ticket scope split stated explicitly when the pattern is used
- [ ] Conflict map present and accurate
- [ ] Dispatch order rationale stated, not just listed (also governs merge order)
- [ ] Decision sheet staged to `/tmp/`, not vault `imports/`
- [ ] Decision sheet ingested as `doc_type=reference_document`
- [ ] `references` edge from decision sheet → methodology steering doc
- [ ] Pilot validation performed end-to-end on first dispatch-order ticket (plan → commit/push/PR → applicable independent review/disposition → merge)
- [ ] Branch-name linchpin verified: pilot's reported `BRANCH` is `codex/<descriptive-slug>` (§6.3)

**Dispatch gate (§7):**
- [ ] DISPATCH GATE block emitted verbatim
- [ ] User authorization received before any post-pilot subagent fires

**Dispatch phase / fan-out (§8):**
- [ ] One active implementation subagent at a time; await and verify completion
- [ ] Subagent prompt rendered from §8.2 template, passed verbatim (no improvisation)
- [ ] Fresh Codex subagent dispatched with `fork_turns="none"`; explicit verified worktree scope
- [ ] STATUS parsed by grepping `^STATUS:`, not by inference
- [ ] On `STATUS: pr_open` → record branch + PR in ledger; no local landing
- [ ] On `STATUS: blocked` → halt; no further dispatches
- [ ] No worktree teardown during fan-out (worktrees accumulate; later safe cleanup requires its scoped grant)
- [ ] Primary checkout HEAD/status preserved; freshly fetched remote default tip unchanged during fan-out

**Merge gate (§9):**
- [ ] MERGE GATE block emitted verbatim after fan-out completes
- [ ] User authorization received before any `/merge` runs

**Merge phase (§§10–11):**
- [ ] Current review/disposition, valid waiver or source-backed no-trigger evidence verified for each exact candidate before `/merge`
- [ ] `/merge` invoked per PR in dispatch order, serially (foundation-first)
- [ ] Each `/merge` keyed off the subagent-reported `BRANCH`, not a reconstructed name
- [ ] Wait for each `/merge`'s terminal state before the next (serial — the next PR's content depends on this one landing)
- [ ] Rebase conflicts: halt-and-surface by default; mechanical policy applied only when explicitly authorized AND the single-commit guard holds
- [ ] Mechanical resolution applied by `/batch` in the worktree (rebase --continue → verify → force-with-lease → re-invoke `/merge`); `/merge` itself never auto-resolves
- [ ] No dispatcher-side worktree teardown (owned by `/merge`)
- [ ] Verified remote merge identity/parent/tip advances per serial landing; no local checkout movement assumed

**Final report (§13):**
- [ ] Per-ticket table + portfolio observations + merge state + close-ticket handoff
- [ ] `/close-ticket` was NOT executed by the skill or by any subagent

**At end:**
- [ ] Every queue entry has a ledger row (ready_for_close / pr-open / blocked)
- [ ] Number of squash commits on main matches number of merged cohort PRs
- [ ] Final `git worktree list` accounts for every cohort worktree; unauthorized or unsafe cleanup remains pending
- [ ] Every ready_for_close ticket is still `lifecycle_status=active` in SAGE

---

## §15 Resume after halt and fallback execution

### §15.1 Conversational resume

When the cohort halts mid-dispatch or mid-merge (§12), the in-session context still holds:

- The full queue (with progress marker).
- The decision-sheet document id.
- The ledger to date (which PRs are open, which are merged).
- The conflict-resolution policy state.

Resume is conversational: the user resolves the underlying issue ("ok, that conflict shape fits the policy", or "add this to the decision sheet", or "skip <ticket-id>, mark it as deferred", or "CI's green now, re-merge"), and the skill picks up from the halted ticket in the same turn. No skill re-invocation needed.

If a halted ticket needs the decision sheet updated, update the vault-resident sheet via a new ingest revision before resuming — the next subagent reads the live sheet.

A cohort parked at the MERGE GATE (§9) resumes the same way: "go ahead and merge" re-enters the §10 loop with every open PR intact.

### §15.2 Per-ticket fresh-session fallback

When the §4.3 disjoint-edit-window invariant cannot be established, the foundation-ticket pattern can't restore it, AND rebase conflicts aren't mechanically resolvable under §11:

- Abandon the batched dispatch pattern for this cohort.
- The user runs each remaining cohort ticket in its own fresh Codex session.
- Each fresh session fetches the latest verified remote default state; `/kickoff` forks the worktree from current `origin/main`, so the PR rebases cleanly.
- The §8.2 per-ticket subagent prompt template is repurposed as the fresh session's opening prompt.

Under the PR model, fork-staleness across tickets is absorbed by the merge queue (or, without one, by an explicit dispatcher-side update) — so the fallback is genuinely a last resort, reached only when edit windows overlap irreducibly. Wall time is longer than batched dispatch, but each ticket runs unattended within its session — the user only orchestrates session starts.

---

## §16 Provenance

Historical source version: 2.4.1. New release version: unset pending owner choice.
This candidate must not be represented as a new released batch version.

Historical genealogy is preserved in [historical provenance](references/batch-history.md).

## Codex dispatch and recovery constraints

The approved cohort decision sheet must state whether it covers implementation, commit/push/draft PR creation and the pilot merge. Structural DISPATCH and MERGE gates remain in force. A child still prepares its ticket-specific kickoff plan; existing approval skips only a repeated approval within its actual scope. Missing caller decisions return `blocked`. If `Auto-commit: no`, stop before commit/push/PR creation regardless of template completion demands.

After a `BEHIND`/`CONFLICT` halt, diagnose before proposing recovery. Conflict markers do not exist until a merge/rebase has actually begun. Rebase/history rewrite requires the existing explicit recovery approval; a BEHIND status alone is not that approval. Verify no active worker owns the worktree, fetch the actual base, inspect local/remote tips, then perform the authorized rebase. For any authorized force-push use an explicit lease tied to the observed remote head. Re-run affected checks and re-establish review/disposition currency after history changes before invoking merge. Do not blindly run `rebase --continue` with no rebase in progress.
