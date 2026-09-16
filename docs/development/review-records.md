# CAS review records

**INACTIVE proposal.** See [authority transition](authority-transition.md).

## Evidence and roles

The shared bundle's `project-policy/references/review-evidence.md` is the single
portable evidence contract; activation requires that reference and its consumers
to be present in the verified bundle. CAS supplies the publication forms below.
Evidence identifies repository, exact base/head or base/candidate tree, cycle and
pass, actual dispatch/completion, requested and observed settings separately,
findings/checks/limitations and disposition-to-result mapping. A copied token is
correlation only. A fresh same-model reviewer is independent when author history
is absent; a different model label alone proves nothing. Unknown runtime settings
stay unknown. Failed, missing, truncated or wrong-candidate records are unresolved.
Assess compatibility explicitly before consuming a legacy format.

The author makes canonical changes and dispositions; the reviewer reads and posts
one findings comment per pass, never repairs or writes tickets. A mechanical merge
consumer verifies evidence without adjudicating findings. Preserve private evidence
in an owner-accessible durable location outside disposable worktrees. Publication
requires the authorized destination and scope.

## Review form

First line: `## PR review — pass <N>`. Then give soundness and the reviewer's blocking
recommendation (the human owns the final blocking decision), review depth, exact
cumulative candidate and dispatch provenance. Each numbered finding uses:

```text
### <N>. <CONFIRMED | PLAUSIBLE | REFUTED | NO VERDICT> — <claim>

**Severity:** <low | medium | high>
Category: <category>
```

Follow with concrete file:line evidence. CONFIRMED means exercised or verified;
PLAUSIBLE names an unexercised mechanism. Close with deliberately excluded work.
Nominate failure records only for branch regressions or confirmed pre-existing
defects that escaped a gate live and in scope at the time; prose/style/convention
findings do not qualify. Include proposed failure_class, severity and caught_by_gate.
The author writes any authorized record and may contest its classification.

## Disposition form and complete repairs

First line: `## Review disposition — pass <N>`. The first body item is:

```text
| # | Verdict | Outcome | Note |
|---|---|---|---|
| <N> | <original verdict token> | <outcome> | <evidence or rationale> |
```

Outcomes are exactly `resolved as recommended`, `resolved differently`, `declined`,
or `ticketed as T-NNNN`. Record every finding, additional fix-pass findings, corrected
claims, authorized record/ticket identities, reviewed revision, resulting revision,
fix commit and post-fix checks. A ticketed CONFIRMED row's Note starts with D1–D4.
An unadjudicated PLAUSIBLE finding may be ticketed without a condition. Default to
repair; D1–D3 and their implementation form belong to [ticket procedures](ticket-procedures.md).

**D4 — Review surface expansion.** At disposition only, a confirmed repair introduces
a new contract/edit surface whose required renewed review cannot finish within the
remaining authorized cycle. Name the new surface and unmet review requirement.
Effort, size or a generic work budget is not D4, and D4 never applies at implementation.

Sweep all analogous consumer sites before calling a repair complete. Search existing
helpers and reuse an equivalent one; document a substantive behavior difference when
a new helper is justified. Keep unrelated edits out of staging. Reassess hook changes
and rerun affected checks after hook modifications.

Batch surviving nonblocking owner decisions once after deduplication against existing
tickets and shipped repairs; raise a blocking design choice promptly. A genuinely new
outcome needs authorization. Refresh the PR body if any claim changes and refresh
release classification after remediation. Revise a ticket whose intended end state
changes before close, preferably before merge. Have the reviewer assess a contested
decline; a contest of the stated ground goes to the owner.

## Proposed coverage and stopping

Substantive workflow, policy, tooling, tests and code require independent cumulative
review. Existing contract/longstanding-defect/struggle triggers remain risk cues.
An editorial exception or exact-scope owner waiver is recorded explicitly.

Adopt the verified shared three-round stopping table: at round 1, no fix converges;
a fix with all-low findings converges; a fix plus above-low findings proceeds. At
rounds 2 and 3, blocking recommendation, failure-record nomination or an in-scope
production-code finding means not settled; otherwise converge. Production excludes
tests, test support, conformance gates, CI, Dockerfiles and schema descriptions.
Account for and test permitted fixes even when no further pass is required. Record
reviewed and resulting revisions separately. Never impose re-review after every fix.
Ask before round 4 and each later round. Resumes/handoffs/fixes do not reset the count.

Every disposition ends with `**Cycle state:** <converged | not settled> — <signal>.`
At not-settled round 2+, repeated CONFIRMED classes at a different site also carry
`**Class:** <class> — the fix addressed <the instance | the class>.`
These are proposed rules only: current work remains governed by live CAS policy.

## Merge consumer and retirement

On separately authorized merge, refuse stale PR bodies (multiple commits with body
still equal to the first commit message), unanswered later review comments, incomplete
evidence or checks not bound to the resulting candidate. Historical review-shaped
comments without the exact heading require owner interpretation; do not guess.
Failed forge reads mean unknown, never absence. Convergence does not authorize merge.

Review measurement ended September 15, 2026. No split/precision measurement line,
exposure count, threshold, tooling-entry write or scheduled evaluation is required.
Preserve historical records and the standing failure log.
