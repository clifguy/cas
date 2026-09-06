# Branch Protection for `main`

This document captures the configured branch-protection state for the `main` branch of the `cas` repository. It is the version-controlled, substrate-recursive home for a methodology setting that GitHub otherwise keeps only in the web UI.

## Intent

- The `main` branch is protected from direct push and from force-push or deletion.
- Changes reach `main` only through pull requests.
- Pull requests must pass all required status checks (below) before merge.
- The repository is single-developer today, so PR approvals are not required (`required_approving_review_count: 0`); the PR workflow exists for the CI gate and the diff-review surface, not for peer review. The CODEOWNERS entry documents ownership intent for the future multi-contributor case.
- Squash is the only permitted merge method, so every change lands on `main` as a single commit whose message is the pull request's title and body.
- Pull requests land without being rebased onto `main` first: the strict up-to-date policy is off. See *Landing model* below.

## Landing model

A pull request merges once its own required checks pass, whether or not its branch is level with `main`. Two CI runs bracket a landing: the pull-request run against the branch, and the `push` run on `main` after the squash.

Three consequences follow, and all three are load-bearing:

- **The strict up-to-date policy is deliberately off.** Holding it on buys one guarantee — that nothing lands untested against the tree it is landing onto — at the price of a rebase, a force-push, and a second full CI cycle on every landing. The guarantee is worth less here than the price: see *What the trade actually costs* below.
- **Textual conflicts still block on their own.** GitHub refuses to merge a pull request whose branch conflicts with the base, and that refusal is independent of this setting. Dropping the strict policy moves exactly one failure class, described next, and no others.
- **The `main` push run is load-bearing rather than redundant.** With no strict policy and no queue, it is the only run that sees the merged tree. See *Required status checks* for what it runs and *Compensating controls* for what depends on it.

### What the trade actually costs

The class that moves is the **semantic conflict**: two pull requests each green in isolation and red in combination, with no textual overlap for the merge to catch. Under a strict policy such a pair is caught before either lands. Without one, the second lands and the `main` run reports it.

The exposure is a function of how branches are cut, not of how many land:

- **Serial work barely exposes it.** Each branch starts from a `main` that already contains the previous landing, so there is no window in which two untested changes coexist.
- **Parallel work exposes it directly.** Several branches cut from a single base and landed in sequence are, by construction, untested against one another until each reaches `main`. That shape is where this document expects a red `main` to originate, and where a reviewer's attention belongs.

### Compensating controls

Because `main` can now go transiently red, two things that previously held by construction are now enforced explicitly:

- **The `push` run on `main` is the detector.** It reports a semantic conflict within one cycle of the landing that introduced it, which is the earliest point at which the merged tree exists to be tested.
- **The cloud deploy must precheck the commit it ships.** A deploy takes `origin/main` HEAD. It used to inherit a green `main` for free, because a strict policy admits nothing else. That inheritance is gone, so the deploy path has to read the CI conclusion for the exact commit it is about to ship and refuse one that is red or still running. That precheck lives in operator-side deploy tooling rather than in this repository, so nothing here enforces it; the requirement is recorded here because this is where the guarantee it replaces was lost.

### Merge queues

A merge queue would supply the strict policy's guarantee without its price, by building a temporary branch holding `main` plus the queued changes and testing *that* tree. It is not available here: GitHub offers merge queues only on repositories owned by an organization, and the rules API rejects the rule on any other repository before it reads the rule's parameters. Transferring the repository to an organization is the only route to one, and that route touches the deploy identity's federated-credential subject, which is why it has not been taken.

**`ci.yml` still answers the `merge_group` event, dormantly and on purpose.** The event never fires without a queue, so the trigger costs nothing; retaining it keeps the workflow ready if the ownership question is ever revisited, and removing it would mean editing the locked trigger set, the secret scan's merge-group arm, and the tests covering both, for no present benefit. The trigger set is locked by [`tests/infra/test_ci_workflow_triggers.py`](../../tests/infra/test_ci_workflow_triggers.py) so it cannot regress silently.

Per push to a pull-request branch there is exactly one CI run, where an unrestricted `push` trigger previously fired a second run on the same commit. The trade is that a branch pushed with no open pull request, and a tag push, get no run at all; opening a draft pull request is enough to get one.

**The `main` push run is deliberately kept.** Three reasons, one of them new. The ruleset grants the repository-admin role an always-on bypass, so a direct push to `main` is reachable and would otherwise receive no CI at all. It is the only run whose coverage artifact is attributable to a `main` commit rather than to a transient branch. And it is now the sole detector for the semantic-conflict class described above. All three depend on that run actually happening, which is why the workflow's concurrency group is keyed per commit for a push rather than per ref: a shared `main` group holds one running and one pending run, so a third landing inside the test job's window would evict the second one's pending run and take away exactly what this paragraph is keeping.

## Required status checks

The following CI jobs from [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) are required to pass on every pull request targeting `main`, and run again on `main` after the squash:

| Job | Purpose |
|---|---|
| `test` | Full pytest suite with coverage (fail-under floor enforced). |
| `lint` | Ruff `check` and `format --check` over the repo. |
| `lint-imports` | `import-linter` contract enforcement. |
| `gitleaks` | Secret-scanning over the commits the event introduced. |
| `eslint` | Frontend eslint (`npm run lint` = `eslint . --max-warnings 0`) over `app/`. |
| `storage tests on the deploy floor (pg16)` | `tests/sage/` plus the BFF session store against PostgreSQL 16, the deployed Flexible Server major. |

**A required context is a job's *display* name, which is its `jobs:` key only when the job sets no `name:`.** The first five are bare keys; the sixth is the `name:` value of the `storage-deploy-floor` job, and naming that job's key here instead would create a required check that never reports — which blocks every merge, since GitHub waits on a context that never arrives. If a job is renamed *or gains or loses a `name:` key*, this document and the ruleset definition must be updated together. Read the live contexts with `gh api repos/<owner>/<repo>/commits/<sha>/check-runs --jq '.check_runs[].name'` rather than inferring them from the workflow.

The deploy-floor job exists because every other Postgres this repository runs against — the `test` job, the image build, and the workstation — is major 17, while the deployed target is Flexible Server 16. A statement valid only on 17 would otherwise pass every gate and fail at deploy.

Unlike the five backend jobs, `eslint` is **path-gated** on `app/**` (via the `paths-filter` job, like `vitest`/`playwright-e2e`): a pull request that touches no `app/` files skips the job, and GitHub counts a skipped required check as satisfied. The gate therefore blocks a merge only when the change can affect frontend lint, while remaining mandatory whenever `app/` is touched.

The `paths-filter` job needs no special checkout depth for this. The filter resolves each event's base itself — the merge group's `base_sha`, the push event's `before` — and fetches that commit when the checkout does not already carry it.

## Required reviews

- **Required approving review count:** `0`. The repository is single-developer; the PR workflow is in place for CI gating and self-review of the diff, not for peer approval.
- **Code-owner review enforcement:** off. The [`.github/CODEOWNERS`](../../.github/CODEOWNERS) entry documents ownership intent for the future multi-contributor case, but no review is enforced today.
- When the repository adds collaborators, raise the required-approval count and enable code-owner review enforcement on the ruleset to activate this gate.

## Activation status

**Active.** Enforcement is in place via the repository ruleset `main-protection`, configured under **Settings → Rules → Rulesets** in the GitHub UI. The ruleset was activated 2026-05-19 after upgrading the account to GitHub Pro (private-repo protection requires a paid plan).

**Bypass:** the Repository-admin role has `always`-mode bypass, so the repository owner can push directly to `main` when needed. Use bypass sparingly; the default path is PR-then-merge.

**Strict up-to-date policy: off.** `strict_required_status_checks_policy` is `false`, which is what makes the landing model above the operative one. Turning it back on would reinstate the rebase-and-wait cycle on every landing; do that only alongside a decision to accept that cost, and update the *Landing model* section in the same change.

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
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": false,
        "required_status_checks": [
          { "context": "test",         "integration_id": 15368 },
          { "context": "lint",         "integration_id": 15368 },
          { "context": "lint-imports", "integration_id": 15368 },
          { "context": "gitleaks",     "integration_id": 15368 },
          { "context": "eslint",       "integration_id": 15368 },
          { "context": "storage tests on the deploy floor (pg16)", "integration_id": 15368 }
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

**What verifies you.** [`.github/workflows/ruleset-drift.yml`](../../.github/workflows/ruleset-drift.yml) reads the live ruleset on a daily schedule and compares it field by field against the *Captured ruleset* block above, failing the run and naming each diverging leaf — a flipped flag inside a rule's `parameters` names that flag, not the rule containing it. The forge-internal identifiers and timestamps the block omits are normalised away, so the capture stays free of them. It needs no stored secret: both rulesets endpoints answer to the built-in Actions token and return every rule. (That was established by a probe run under `contents: read` **and** `pull-requests: read`, one notch wider than the `contents: read` this workflow declares. Rulesets are not a pull-request resource, so the narrower scope should behave identically; the first scheduled run settles it, and settles it loudly.) The same comparison is available locally while editing this document, as `python -m scripts.check_ruleset_drift` or as the opt-in test tier `SAGE_TEST_LIVE_RULESET=1 pytest tests/infra/test_ruleset_drift.py`; the default test job runs neither, because `SAGE_TEST_LIVE_RULESET` is unset there.

**One field the scheduled run does not cover: `bypass_actors`.** GitHub withholds it below administration scope, which is not a permission a workflow can declare, and it withholds it by omitting the key rather than returning an empty list. The check keys on exactly that difference — an absent key is reported as *not compared* on every run, while an empty list is a real value and is compared — so "the run could not see the bypass grants" never reads as "there are none". Who may bypass this ruleset therefore stays a review concern under the scheduled run. A local run or the opt-in tier, executed with a maintainer's own credential, does compare it — but only its *presence, count, and modes*. Which built-in role holds a grant is carried by `actor_id`, which the block omits above and the public-posture gate keeps out of this file, so two grants differing only in role are indistinguishable: a grant added, revoked, or re-moded is caught at that scope, and a same-mode swap of one role for another is caught at no scope. Confirm the granted role in **Settings → Rules → Rulesets** when it matters.

The check covers captured-versus-live *state*, not the prose around it. This document has previously asserted a landing model that could not exist on this repository, and given activation instructions that fail against the rules API, while the JSON block stayed accurate throughout — no captured-to-live comparison would have caught either. Prose that misdescribes reality remains a review concern rather than a gated one.

The two directions fail differently. A change made here and not applied to the ruleset leaves the repository under-protected relative to what this file claims. A change made in the UI and not captured here leaves this file asserting something untrue, which is worse, because the claim to be the source of truth is what the rest of this document rests on. The scheduled check is aimed at that second direction, which is the one that carries no commit for an offline gate to hang off.

## Rationale

This file exists because a substrate-recursive principle applies: methodology discipline should live where it survives any single GitHub session, not in a web-UI configuration that disappears from the agent's view the moment the tab closes. Branch protection is methodology — it encodes a discipline gate on merges — and so its configuration belongs in version control alongside the CI workflow it gates.
