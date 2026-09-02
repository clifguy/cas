# Branch Protection for `main`

This document captures the configured branch-protection state for the `main` branch of the `cas` repository. It is the version-controlled, substrate-recursive home for a methodology setting that GitHub otherwise keeps only in the web UI.

## Intent

- The `main` branch is protected from direct push and from force-push or deletion.
- Changes reach `main` only through pull requests.
- Pull requests must pass all required status checks (below) before merge.
- The repository is single-developer today, so PR approvals are not required (`required_approving_review_count: 0`); the PR workflow exists for the CI gate and the diff-review surface, not for peer review. The CODEOWNERS entry documents ownership intent for the future multi-contributor case.
- Squash is the only permitted merge method, so every change lands on `main` as a single commit whose message is the pull request's title and body.
- Pull requests land through a **merge queue** rather than by rebasing each branch onto `main` before merging. See *Merge queue* below, and *Activation status* for which half of it is live.

## Merge queue

The queue builds a temporary branch holding `main` plus the queued changes, runs the required checks against **that** tree, and merges only if they pass. It supplies the same guarantee as *require branches to be up to date* — nothing lands untested against the tree it is landing onto — without a manual rebase, a force-push, and a second full CI cycle per landing.

Two consequences follow, and both are load-bearing:

- **`ci.yml` must answer the `merge_group` event.** Required status checks are satisfied from the merge-group run, not from the pull-request run. A required check that no merge-group run reports never arrives at all, and the queue dequeues the pull request when its check-response timeout expires. The trigger set is locked by [`tests/infra/test_ci_workflow_triggers.py`](../../tests/infra/test_ci_workflow_triggers.py) so it cannot regress silently.
- **The strict up-to-date policy comes off.** It is redundant beside the queue, and holding both would reinstate the rebase-and-wait cycle the queue exists to remove.

Per push to a pull-request branch there is now exactly one CI run, where an unrestricted `push` trigger previously fired a second run on the same commit. Per landing there are two: the merge-group run, and the `push` run on `main` after the squash.

**The `main` push run is deliberately kept** rather than dropped as redundant with the merge-group run. Two reasons. The ruleset grants the repository-admin role an always-on bypass, so a direct push to `main` is reachable and would otherwise receive no CI at all. And it is the only run whose coverage artifact is attributable to a `main` commit rather than to a transient queue branch. The cost is one run per landing, against a change that already removes both the rebase cycle and the duplicate per-push run.

## Required status checks

The following CI jobs from [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) are required to pass on every pull request targeting `main`, and again on the merge group that lands it:

| Job | Purpose |
|---|---|
| `test` | Full pytest suite with coverage (fail-under floor enforced). |
| `lint` | Ruff `check` and `format --check` over the repo. |
| `lint-imports` | `import-linter` contract enforcement. |
| `gitleaks` | Secret-scanning over the full commit history. |
| `eslint` | Frontend eslint (`npm run lint` = `eslint . --max-warnings 0`) over `app/`. |

These five names match the `jobs:` keys in `ci.yml` verbatim. If a job is renamed, this document and the corresponding ruleset definition must be updated together.

Unlike the four Python jobs, `eslint` is **path-gated** on `app/**` (via the `paths-filter` job, like `vitest`/`playwright-e2e`): a pull request that touches no `app/` files skips the job, and GitHub counts a skipped required check as satisfied. The gate therefore blocks a merge only when the change can affect frontend lint, while remaining mandatory whenever `app/` is touched.

That "skipped counts as satisfied" rule is also why the `paths-filter` job checks out with `fetch-depth: 0`. On a merge-group run the filter resolves the group's base commit from the local object store and does not fetch it itself; under a shallow checkout that object is absent, the filter mis-reports, and `eslint` is skipped — reported as satisfied — on every queued landing. The bypass would be green, so it is locked by a gate test rather than left to review.

## Required reviews

- **Required approving review count:** `0`. The repository is single-developer; the PR workflow is in place for CI gating and self-review of the diff, not for peer approval.
- **Code-owner review enforcement:** off. The [`.github/CODEOWNERS`](../../.github/CODEOWNERS) entry (`* @clifguy`) documents ownership intent for the future multi-contributor case, but no review is enforced today.
- When the repository adds collaborators, raise the required-approval count and enable code-owner review enforcement on the ruleset to activate this gate.

## Activation status

**Active.** Enforcement is in place via the repository ruleset `main-protection`, configured under **Settings → Rules → Rulesets** in the GitHub UI. The ruleset was activated 2026-05-19 after upgrading the account to GitHub Pro (private-repo protection requires a paid plan).

**Bypass:** the Repository-admin role has `always`-mode bypass, so the repository owner can push directly to `main` when needed. Use bypass sparingly; the default path is PR-then-merge.

**Merge queue:** the CI side is in place — `ci.yml` answers `merge_group`, and the `paths-filter` job checks out full history so the filter resolves a merge group's base commit. The ruleset's `merge_queue` rule and the removal of `strict_required_status_checks_policy` are applied separately, in that order, and the block below is re-captured from the live ruleset when they are. Enabling the rule before CI could answer the event would stall every open pull request until the queue's check-response timeout, which is why the two land apart.

The captured ruleset JSON is reproduced under *Captured ruleset* below as the source of truth; the GitHub UI is its reflection.

## Captured ruleset

The following is the live ruleset definition (captured via `gh api repos/<owner>/<repo>/rulesets/<ruleset-id>`), reproduced here so the configuration survives any GitHub session. If the UI and this block diverge, treat this block as the source of truth and reconcile the UI.

```json
{
  "name": "main-protection",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] }
  },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "do_not_enforce_on_create": false,
        "required_status_checks": [
          { "context": "test",         "integration_id": 15368 },
          { "context": "lint",         "integration_id": 15368 },
          { "context": "lint-imports", "integration_id": 15368 },
          { "context": "gitleaks",     "integration_id": 15368 },
          { "context": "eslint",       "integration_id": 15368 }
        ]
      }
    },
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": false,
        "required_reviewers": [],
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false,
        "require_extra_approval_for_unattributed_changes": true,
        "allowed_merge_methods": ["squash"]
      }
    }
  ],
  "bypass_actors": [
    { "actor_type": "RepositoryRole", "bypass_mode": "always" }
  ]
}
```

The bypass actor is the Repository-admin role (`actor_id` omitted — it is a GitHub-internal numeric id that varies per repository). `~DEFAULT_BRANCH` targets `main` and survives any future default-branch rename.

## Reconciliation procedure

When this document changes (a new status-check job is added, a job is renamed, the review policy changes, etc.), update the ruleset in lockstep:

1. Edit this document.
2. Apply the corresponding change in the GitHub UI under **Settings → Rules → Rulesets → `main-protection`** (or via `gh api --method PATCH repos/<owner>/<repo>/rulesets/<ruleset-id>`).
3. Re-capture via `gh api repos/<owner>/<repo>/rulesets/<ruleset-id>` and update the JSON block above.
4. Commit both the document and (if applicable) the workflow change in the same commit.

Treat the document as the source of truth and the UI as its reflection.

## Rationale

This file exists because a substrate-recursive principle applies: methodology discipline should live where it survives any single GitHub session, not in a web-UI configuration that disappears from the agent's view the moment the tab closes. Branch protection is methodology — it encodes a discipline gate on merges — and so its configuration belongs in version control alongside the CI workflow it gates.
