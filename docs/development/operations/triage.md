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

Classify a ticket with another worker's branch/worktree or open PR as claimed
(in flight, or claimed idle when activity is not current), not ready to start again.
Classify verified merged work as landed and open; merge evidence is not acceptance
or permission to close. Keep both categories out of fresh-start recommendations.
Only unclaimed, unlanded, on-horizon work with satisfied prerequisites is ready.
Missing claim/landing evidence after a failed lookup is unknown, not unclaimed.

Separate ready, claimed, landed, blocked, deferred and unknown. Rank ready work by priority
(high before medium before low), then descending open-ticket unblocking fan-out,
then audience (caller before internal), reach (fundamental before narrow), then
ticket_id ascending (the existing age tie-break). Count fan-out as distinct active
ticket identities that directly list the candidate as an unsatisfied effective
blocker, not version rows, duplicate edges or transitive descendants. Resolve
retractions and lineage before counting; incomplete evidence leaves the count unknown.
Missing impact values rank at the lower value and are reported as missing, not
silently filled. Do not replace the stable ID order with opening-date order.
Keep deferred work off the active horizon without changing its priority.

For concurrent candidates normalize scope and check parent/child paths, renames and
shared invariants/contracts. Unequal strings do not prove nonoverlap. Incomplete
lineage, scope or remote evidence yields an explicit unknown. Explain recommendations
with the evidence that determined order. Do not write tickets, edges, metadata,
lifecycle, branches or schedules.
