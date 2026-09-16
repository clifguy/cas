# Development procedure candidates

**INACTIVE proposal.** These documents do not govern current work. The existing
repository guide, canonical code-review procedure and live SAGE authorities remain
effective. No guide pointer, installation or vault authority changes here.

## Proposed bindings

This is a human-readable binding proposal, not resolver input. Activation must
translate it into the supported project-policy schema and verify actual selection.
Do not invent a `next` resolver operation.

| Existing operation | Required supplementary CAS procedure |
|---|---|
| kickoff | checks.md; release.md; ticket-procedures.md |
| commit | checks.md; release.md; existing canonical cas-code-review |
| review-pr | review-records.md; existing canonical cas-code-review |
| converge-review | review-records.md |
| disposition | review-records.md; ticket-procedures.md; release.md; checks.md |
| merge | review-records.md; release.md |
| ticketing | ticket-procedures.md; operations/triage.md for the explicit read-only triage entry |
| smoke-test | operations/smoke-test.md |

The triage entry point resolves `ticketing` and explicitly selects the required
`operations/triage.md` supplement for its read-only path. An unsupported route blocks that consumer until
an approved binding exists. It does not authorize a resolver schema extension.

## Authority receipt and bounded replacement

Refreshed September 15, 2026 through the local SAGE deployment serving build
`2.5.0+aba8bb7`. Its live inventory includes `cas`; live configuration declares
ticket audience/reach and `supersedes`. Current head candidates had no inbound
supersedes successors on refresh:

| Authority | Head identifier | Candidate treatment |
|---|---|---|
| PR Review Conventions v18 | `2fc3d182_cas_pr_review_conventions` | Preserve forms; propose expanded coverage and shared three-round rule |
| Ticket Conventions v29 | `fc229ba2_cas_ticket_conventions` | Preserve CAS schema, graph and deferral obligations |
| Release Classification v2 | `29251490_cas_release_classification` | Bind existing classifier and owner release boundary |
| Invocation Authority v1 | `a499338f_cas_invocation_authority` | Preserve named authorization boundaries |
| review-pr tooling v16 | `76020a58_review_pr_independent_pr_stage_review_gate` | Measurement retired; gate and failure log retained |

Read these through current live lineage before activation. Identifiers here are
receipts, not permanent head selectors. Complete source captures and execution
evidence remain outside this candidate; publication receipts identify their hashes.

The old gate-status measurement procedure is retired, not migrated: no exposure
counter, threshold, scheduled evaluation or retrospective is revived. Preserve
historical captures and existing installations. Standing failure records continue.

## Activation and rollback

Separate owner approval must name the coordinated CAS revision, compatible shared
bundle and manifest, live-authority transition, installation paths and rollback.
Pause affected operations, back up the complete installation, then change authority
pointers and any approved SAGE supersessions together. Verify one effective route
from a fresh context, complete dependency inventory and runtime behavior before
resuming. A source PR is not installed or activated acceptance.

Keep the working transfer dependency until external SAGE integration passes its
separate acceptance. CPML-finalize is excluded. Broader batch/deploy/code-review
relocations are excluded; canonical code review stays where it is. Rollback restores
the complete compatible consumer/policy set and explicitly reinstates authority;
never mix selected old and new files. No merge, deployment, release/tag, service
write, cleanup or ticket closure is authorized by these candidates.
