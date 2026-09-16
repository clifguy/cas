# CAS read-only triage

**INACTIVE proposal.** See [authority transition](../authority-transition.md).

Use a supported project-policy read route and the explicit CAS procedure binding;
never pass an unsupported `next` operation. Read live inventory/configuration and
the [ticket procedure](../ticket-procedures.md), then enumerate the requested scope
with complete pagination. Do not turn a read/list into an unrequested repair audit.

Resolve ticket identities across complete lineage and effective dependencies,
including retractions, valid predecessor anchors and completed blockers. Read
prerequisite verification alongside ticket body gates, owner decisions, dates and
required measurements. Corroborate claimed shipped work against current PR/CI and
worktrees; failed remote lookups mean unknown, not absence. Never infer identity
from slugs or rewrite graph anchors while triaging.

Separate ready, blocked, deferred and unknown. Rank ready work by priority
(high before medium before low), then descending open-ticket unblocking fan-out,
then audience (caller before internal), reach (fundamental before narrow), then
oldest opening date. Missing impact values rank at the lower value and are reported
as missing, not silently filled. Report ties without inventing an urgency distinction.
Keep deferred work off the active horizon without changing its priority.

For concurrent candidates normalize scope and check parent/child paths, renames and
shared invariants/contracts. Unequal strings do not prove nonoverlap. Incomplete
lineage, scope or remote evidence yields an explicit unknown. Explain recommendations
with the evidence that determined order. Do not write tickets, edges, metadata,
lifecycle, branches or schedules.
