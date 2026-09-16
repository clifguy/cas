# CAS read-only triage

Selected candidate procedure. The required activation receipt blocks operational use until the coordinated cutover; see [authority transition](../authority-transition.md).

Use a supported project-policy read route and the explicit CAS procedure binding;
never pass an unsupported `next` operation. Read live inventory/configuration and
the [ticket procedure](../ticket-procedures.md), then enumerate the requested scope
with complete pagination. Do not turn a read/list into an unrequested repair audit.

Resolve the deployment/vault namespace independently of the requested project
filter. Project filters narrow presentation, never the meaning of an identity or a
dependency into another project in that vault. Do not borrow fields or rankings
from the hosted software-development portfolio: CAS uses its own live schema and
the ordering below. A missing binding or unavailable required source blocks the
recommendation; do not silently apply generic ranking or another project's policy.

Resolve ticket identities across complete lineage and effective dependencies,
including retractions, valid predecessor anchors and completed blockers. Read
prerequisite verification alongside ticket body gates, owner decisions, dates and
required measurements. Corroborate claimed shipped work against current PR/CI and
worktrees; failed remote lookups mean unknown, not absence. Never infer identity
from slugs or rewrite graph anchors while triaging.

Dependencies point from dependent to prerequisite. Inspect effective graph results
and literal rows when deduplication masks siblings; validate both anchors in their
lineages and apply retractions before deciding or counting. A dependency anchored
at a valid completed predecessor can be satisfied without moving it to the active
successor. A dropped prerequisite is not automatically completed. Cycles, missing
versions, truncated traversal and unresolved anchors remain blocked or unknown with
the distinction explained. Do not invent a missing edge or repair a stale one here.

Read the complete ticket body, not only metadata or abstract, before declaring it
ready. A future date, elapsed observation window, required deployment, outstanding
owner choice or measurement threshold is an independent gate even with no graph
blockers. Use an explicit as-of time/timezone for date gates. Satisfied graph
prerequisites do not satisfy body gates. Unknown body/time/measurement evidence
produces unknown readiness; a verified unmet gate is blocked, and explicit deferred
horizon remains deferred.

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

Missing priority cannot be invented or silently ranked low. Missing fan-out evidence
cannot be treated as zero. Separate such uncertain ordering from a verified total
order; state which recommendation could change after the missing evidence arrives.
The ID tie-break compares the numeric portion of canonical schema-valid identities,
not filenames, search relevance or page order.

For concurrent candidates normalize scope and check parent/child paths, renames and
shared invariants/contracts. Unequal strings do not prove nonoverlap. Incomplete
lineage, scope or remote evidence yields an explicit unknown. Explain recommendations
with the evidence that determined order. Do not write tickets, edges, metadata,
lifecycle, branches or schedules.

Report each recommended candidate's priority, direct distinct-identity fan-out,
audience, reach and numeric age tie-break, plus claim/landing/body/dependency evidence.
For a parallel recommendation include a pairwise scope assessment. Normalize against
the same repository root, include both rename endpoints, parent/child containment
and shared contract/invariant edits even across disjoint paths. Missing paths are
unknown overlap, never proof that parallel work is safe.
