# CAS ticket procedures

**INACTIVE proposal.** See [authority transition](authority-transition.md).

Resolve the deployment from live inventory, then read the `cas` vault configuration
and current Ticket Conventions through supersession lineage. The route uses project
`CAS`, doc_type `ticket`; it does not export CAS fields to other vaults. Shared
consumers use the actual project's schema, including vaults without audience/reach.

## Metadata and identity

Preserve original submitter, opening date, history, title identity, tags and complete
tier3 metadata on source supersession: ticket_id, type, priority, horizon, audience,
reach and any closure provenance. Types are feature/fix/methodology/spike/doc;
priority high/medium/low is urgency; horizon current/deferred is scheduling, not
lifecycle. Absence of horizon means current. Completed/dropped tickets have no horizon.
Every active CAS ticket carries audience and reach, although the schema permits
their absence for historical records. Do not invent missing priority.

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

Use retained source bytes for substantive revisions and atomic predecessor-based
ingest with optimistic concurrency. Explicitly supply metadata (it does not inherit).
Wait for terminal pipeline status and restore the prior completed or dropped resting
state through valid lifecycle transitions within the authorized revision scope.
Read back metadata, lineage, pipeline and resting state. Do not manufacture a version
chain with separate edge creation and archival writes.

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
