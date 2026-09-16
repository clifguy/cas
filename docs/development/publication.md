# CAS publication procedure

**INACTIVE proposal.** See [authority transition](authority-transition.md).

## Push

Resolve push policy separately from commit in the intended worktree. Verify the
configured remote, feature branch, approved publication scope and exact committed
candidate. Preserve the shared Git refusal rules; never force-push or publish a
different dirty/index candidate because review passed elsewhere. Bind completed
review/check evidence to the outgoing head and report unresolved gates truthfully.
Normal publication may start CI concurrently with independent review; pending CI
is not a pass. Read back the remote branch head after a successful push. An
uncertain network result requires reconciliation before retry.

## Pull request

Resolve pull-request policy separately. Verify the remote repository, base and
head, then create or update the authorized draft PR. Summarize the final cumulative
behavior, validation and material limits, including inactive status and unmet
cross-repository dependencies. Include the current `Release classification:` line
from [release](release.md), without invoking release machinery.

Read back PR number/URL, draft status, base/head and body. Refresh the body when
remediation invalidates a claim. [Review records](review-records.md) owns review and
disposition publication formats and exact-candidate correspondence. No stale body,
missing review evidence or unrelated green CI may become a readiness claim.
Publication ends at the draft/review boundary; it never implies merge, activation,
deployment or closure permission.
