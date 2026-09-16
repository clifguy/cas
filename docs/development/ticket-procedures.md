# CAS ticket procedures

Selected candidate procedure. The required activation receipt blocks operational use until the coordinated cutover; see [authority transition](authority-transition.md).

Resolve the deployment from live inventory, then read the `cas` vault configuration
and current Ticket Conventions through supersession lineage. The route uses project
`CAS`, doc_type `ticket`; it does not export CAS fields to other vaults. Shared
consumers use the actual project's schema, including vaults without audience/reach.

## Route and operation boundary

Resolve `ticketing` in the intended checkout. Its required supplement is this file;
the explicit read-only triage consumer additionally selects
`operations/triage.md`. That extra selection is a required, source-backed instruction
binding in its resolution evidence, not a new resolver operation or a default for
writes. No active profile or registry is installed by this proposal.

The namespace key is the verified deployment identity plus vault id. Project label,
repository, checkout, doc_id and source slug are not allocation namespaces. Multiple
projects in the same vault share its ticket sequence; an identically named vault on
another deployment does not. Resolve any legacy counter's actual deployment binding
before using it. An ambiguous old counter is not portable evidence. Its migration
and registry adoption remain separate owner decisions.

Use the shared ticket-management workflow and its existing SAGE access mechanism;
this supplement introduces no storage client. Resolve current policy through live
lineage for writes; routine reads do not trigger a hygiene audit. Create, revise,
link, archive and close retain their distinct authorization boundaries. A close
request never grants unrelated cleanup or permanent authority to close later work.

## Metadata and identity

Preserve original submitter, opening date, history, title identity, tags and complete
tier3 metadata on source supersession: ticket_id, type, priority, horizon, audience,
reach and any closure provenance. Types are feature/fix/methodology/spike/doc;
priority high/medium/low is urgency; horizon current/deferred is scheduling, not
lifecycle. Absence of horizon means current. Completed/dropped tickets have no horizon.
Every active CAS ticket carries audience and reach, although the schema permits
their absence for historical records. Do not invent missing priority.

The actual tier3 keys are `ticket_id`, `ticket_type`, `ticket_priority`,
`ticket_horizon`, `ticket_audience`, `ticket_reach`, `close_commit` and `close_pr`.
Use the live schema rather than this list if it changes. Creation supplies `project`,
`doc_type`, title prefixed with the allocated identity, original `document_date`,
`ticket` tag, and complete schema-valid tier3 values. The body states intended work
and independently verifiable acceptance; add design notes only when necessary.
Metadata-only changes use ops-object patches; status-only changes use configured
lifecycle actions. Neither justifies a new body version.

Audience is caller for MCP/REST responses, published specifications, app UI or deployed
durability/availability; internal for contributor tests, gates, tooling, pins and
housekeeping. Classify the deliverable, not what it ultimately protects. Reach is
fundamental for a relied-on contract/invariant/seam/gate, narrow for a single edge
case/message/false positive/cosmetic change. File count is not reach.

Identity is metadata plus complete supersession lineage, never a slug or row count.
Allocate against the full namespace, including terminal versions, with the active
allocator and server uniqueness guard; an uncertain write is reconciled before retry.
Do not silently switch existing allocation/registry installations during preparation.
Preserve follow-up tags and source-of-work references; they are not redundant with
dependency edges. Collision or hygiene repair needs its own scoped evidence and must
not silently rename identities while listing a portfolio.

### Allocation and source filenames

The allocator's cached candidate is only a hint. Check it against all lifecycle
states. A stale/missing hint requires every page of the full ticket catalog,
including completed and dropped heads and superseded history. Do not filter by
project when computing the vault-wide maximum. Reject malformed identities or
incomplete pagination rather than skipping them. The candidate is the maximum
numeric identity plus one, or the first schema-valid identity for an empty vault.
Do not wrap an exhausted schema range. The server's configured atomic uniqueness
guard is required; a client-side empty lookup is not race protection.

On `tier3_unique_constraint_violation`, rerun existence/full-history allocation and
retry the ingest once. A second collision stops and reports the conflict. An
ambiguous timeout first requires reconciliation against observed records and
pipeline outcome; it is not a confirmed collision or permission to allocate again.
Advance a verified cache only after successful creation, never after failure or an
unreconciled response. Preserve the existing installed allocator until separate
adoption; this proposal does not silently rewrite its state layout.

Read the complete source/version history before naming a substantive revision.
Keep the semantic slug stable and choose a version suffix above every historical
version used for that identity, including archived predecessors and a higher old
version than the current head label. Never count active rows, count chain members,
or increment only the head label to obtain that suffix. Existing unversioned source
names remain historical evidence; first versioned revision must avoid every occupied
name. Ambiguous, missing or unparseable history blocks name selection. Retained
source bytes, not a reconstructed search snippet, are the edit baseline.

Use retained source bytes for substantive revisions and atomic predecessor-based
ingest with optimistic concurrency. Explicitly supply metadata (it does not inherit).
Wait for terminal pipeline status and restore the prior completed or dropped resting
state through valid lifecycle transitions within the authorized revision scope.
Read back metadata, lineage, pipeline and resting state. Do not manufacture a version
chain with separate edge creation and archival writes.

Capture the current head/version and use the served optimistic precondition with
`predecessor_id`; on conflict reread and rebase the intended edit, never overwrite a
concurrent revision. Preserve full metadata explicitly, including submitter where
stored, original opening date, follow-up tags and closure provenance. The CAS live
table permits completed predecessors to be superseded directly; after terminal
pipeline success complete the new head. It does not permit archived predecessors:
when that revision is authorized, reactivate the dropped head, supersede it, then
archive the successful new head. Do not archive a completed predecessor merely to
reactivate it. If interrupted, reconcile the actual head before restoring state;
report failed/nonterminal pipeline or unfinished restoration separately and do not
declare the revision complete. These restoration actions preserve the prior decision
and do not authorize reopening work or making a new acceptance judgment.

## Dependencies and triage

An A depends_on B edge means B must finish before A. Resolve complete chains,
anchor validity and retractions using the served graph contract; a completed
predecessor may satisfy an edge whose anchor remains valid. Never reanchor a valid
predecessor edge merely because a successor exists. Retractions suppress dependencies;
incomplete lineage yields unknown. Inspect literal rows where traversal deduplicates.
Unequal path strings do not prove disjoint edits: normalize, compare parent/child
scopes, include renames and shared contracts. Missing scope evidence means unknown.
The [triage procedure](operations/triage.md) is read-only.

## Deferral and closure

Repair confirmed in-scope findings by default. Effort is not deferral. Scope growth
within the approved outcome is repaired and reflected in the ticket; a genuinely new
outcome or owner design decision requires authorization. The allowed conditions are:

- D1 — Caller-owned decision: interface, convention or tradeoff reserved to the owner.
- D2 — Outside the edit window: disjoint repair or a contract the diff has not crossed.
- D3 — Verifiable only elsewhere: the necessary gate/deployment/environment is unavailable.

Name a condition before asking about deferral. Batch deduplicated nonblocking decisions;
interrupt promptly for a real blocker. A ticket written at the implementation
findings halt carries, as its first body line under the title, exactly one of:

```text
**Deferral:** D<N> — <condition and evidence, one line>
**Deferral:** none — <why this ticket is not a deferred finding, one line>
```

Use D1–D3 for an actual deferred finding. The `none` arm covers a planned increment
that defers no finding; do not disguise a deferral with it. This form does not
extend to tickets written after the origin merged or between disposition and merge.
Disposition uses the review table's
Note column and its stage-specific D4. Preserve the live exception for unadjudicated
PLAUSIBLE findings. Writing tickets still requires the operation's authorization.

Closure requires explicit owner initiation and verified acceptance, not merely merge.
close_commit is the actual landed SHA; close_pr is the convenience PR number. For
non-commit outcomes use appropriate source references, not invented SHAs. Clear
obsolete horizon metadata, verify resulting state, and report cleanup separately.

Before close, read the current head's complete acceptance and every body gate, and
verify evidence against that head. If implementation expanded the intended end state,
revise acceptance under the authorized scope before declaring it satisfied. A merged
PR is evidence of landing only: installation, activation, deployment, observation
windows and measurements may still be required. Missing evidence leaves closure
blocked. An already completed target is an idempotent readback, not permission to
reopen it or repeat writes. Conflicting provenance is reported rather than replaced.

Record the actual landed SHA in `close_commit` and the corresponding PR number in
`close_pr`; leave both null for a non-commit resolution and verify the appropriate
reference edge. Unset `ticket_horizon` only when present. Then perform the configured
completion action and read back lifecycle and provenance. A metadata patch and
lifecycle transition are separate operations: if either fails or times out, report
the partial result and reconcile before retrying. Branch/worktree cleanup is a
separate authorized operation; cleanup failure cannot turn verified closure into
non-acceptance or justify repeating closure writes.
