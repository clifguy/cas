# Deploy

**INACTIVE proposal.** This staged procedure does not govern current work or authorize execution.
Resolve current project policy and caller authorization before adoption or use.
Read [Codex runtime](references/codex-runtime.md) and [dependencies](references/dependencies.md).

Drive a CAS cloud-profile tenant deploy from "operator wants to ship" to "the CI deploy run
finished and here is its result." The skill is the trigger-and-watch surface over the authoritative
deploy path: a single `workflow_dispatch` on `.github/workflows/infra.yml`, parameterized by the
tenant's GitHub Environment, that runs the full staged bring-up end to end in CI (CAS-ADR-042; the
*CAS Cloud Deployment Discipline*, Principle 1 — "deploys run from committed code, through CI").

**Invoking `/deploy <domain>` is the standing authorization to dispatch that tenant's production
deploy.** The skill fires once the prechecks pass — there is no second confirmation gate. The
safety valve is `--dry-run`, which runs the prechecks and prints exactly what would be dispatched
without firing.

This skill drives a **redeploy of an already-configured tenant**. First-time bring-up is out of
scope (see *Out of scope*); the skill halts if the tenant's Environment does not exist.

## Inputs

`/deploy <domain> [--dry-run] [--allow-red-main]`

- `<domain>` — the tenant domain. Resolved to a GitHub Environment via the map below.
- `--dry-run` — inspect preconditions once, report CI readiness and the would-be command, then stop without waiting or dispatching.
- `--allow-red-main` — explicitly bypass a known non-success, pending or absent CI run with a visible warning. It does not bypass failed API/auth reads or other preconditions.

### Domain → GitHub Environment map

| Domain | Environment |
|---|---|
| `cor.org` | `cor-prod` |

This table is the extension point: adding a tenant is adding a row here (and authoring its GitHub
Environment + parameter set, which is out of scope for this skill). A domain not in the table is a
hard halt — never guess an Environment name.

If `<domain>` is missing, ask the caller which tenant to deploy (one focused question), listing the
known domains.

## Preamble — anchor gh to the cas repo

`gh` infers the repository from the current directory's git remote. Codex execution calls need an
explicit working directory; directory changes and shell variables may not persist between calls. Resolve the repo slug once and thread it through every `gh` call as
`-R "$repo"` so no call depends on the ambient cwd:

```
repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)
```

Run that from the cas checkout. If it fails (not in the cas repo / gh not authenticated), halt — see
Step 1. Every `gh` command below is `gh -R "$repo" …`.

## Workflow

### Step 0 — Resolve the tenant Environment

Parse `<domain>` from the arguments and look it up in the map. On a miss, halt:
`Unknown tenant domain '<domain>'. Known: cor.org.` Detect the `--dry-run` flag.

Resolve `env` (e.g., `cor.org` → `cor-prod`). Detect the `--allow-red-main` flag alongside
`--dry-run`.

### Step 1 — Preconditions

1. **gh auth.** `gh auth status` must succeed. If not, halt: `gh is not authenticated; run 'gh auth login'.`
2. **The deploy ships committed `origin/main`.** CI always deploys `origin/main` HEAD, never a local
   working tree (Discipline Principle 1). `git fetch origin main`, then capture the SHA and subject
   that will deploy: `git rev-parse --short origin/main` and `git log -1 --format='%s' origin/main`.
   Report them — the operator should see exactly which commit is going out.
3. **Wait for the exact commit's CI.** Capture the full `origin/main` SHA from the successful fetch in item 2. Only a completed, successful `ci.yml` **push-to-main** run for this exact SHA satisfies the gate. A PR-head or merge-group run is not a substitute. Query explicitly:

   ```
   gh -R "$repo" run list --workflow ci.yml --branch main --event push --commit "$full_sha" --limit 10 --json databaseId,headSha,event,status,conclusion,url,createdAt,attempt
   ```

   Verify the returned SHA/event and select the newest matching run, retaining its `databaseId`, attempt and URL. Re-read its status with `gh -R "$repo" run view "$CI_RUN_ID" --json headSha,event,status,conclusion,url,attempt`; verify the identity on every read. An older successful run must not mask a newer queued or failed run/attempt. Re-query the exact-SHA list before accepting success to detect a newer run or rerun.

   **Normal invocation:** if CI is queued, requested, waiting, pending or in progress, announce once that deployment is waiting, with the commit and run URL. Poll about every 30 seconds, using the silent-loop discipline below. Continue automatically when CI succeeds; no second confirmation is needed. If no matching run has appeared yet, poll discovery for up to 120 seconds to accommodate the delay after merge. Absence after that window is a halt, not permission to deploy.

   On first entry to this precheck, record its start time, even if the first run is already green. Use a 3000-second wall-clock deadline from that time for the entire prerequisite wait, including discovery, retries and any changed-main checks. This is a bounded wait, not a promise that queued CI will finish in that time. Do not reset the deadline after a new attempt or main revision; each new SHA may get its own discovery grace only within the original total deadline. On each tick evaluate:

   - **Completed with success:** revalidate the newest run/attempt and proceed to the pre-dispatch revision check below.
   - **Completed with any other conclusion:** halt immediately with `CI_FAILED`, the exact conclusion and URL. Failure, cancelled, timed_out, startup_failure, skipped, neutral, stale or an empty/unknown conclusion are not success. Never cancel or rerun CI automatically.
   - **Action required:** halt with `CI_ACTION_REQUIRED` and the run URL; a person must resolve it.
   - **Known nonterminal state:** continue until the deadline; then halt with `CI_PENDING`, elapsed time and URL. CI remains running; no deploy has been dispatched. Re-invocation can resume observing it.
   - **No run after the discovery window:** halt with `CI_MISSING` and the exact SHA.
   - **Failed API/auth/parse read, identity mismatch or unrecognized status:** do not treat it as pending, absent or green. Retry transient reads at most three consecutive times within the same deadline, then halt with `CI_LOOKUP_FAILED` and the diagnostic. Authentication failures and identity mismatches halt immediately.

   **Silent-loop mechanics for Codex:** use an available execution tool that returns a running session (for example `exec_command` with a short `yield_time_ms`), then await/poll that session using its supported continuation tool. Set all required repository, SHA, run and deadline values in that execution or pass them explicitly; do not rely on prior shell variables. The loop emits no per-tick stdout/stderr, but retains diagnostics and emits one terminal result with run identity. Do not use Claude-only background-tool arguments or assume process completion will automatically wake a finished assistant turn. Keep the task active until the result is received. Bounded waits of at most 60 seconds between assistant opportunities preserve cancellation and meaningful progress updates; do not narrate unchanged ticks. If the user cancels the wait, stop the local watcher and do not dispatch or cancel the remote CI run.

   **Dry-run:** perform one exact-SHA lookup and report green, pending, missing, failed or unavailable, plus the run URL when present. Do not start the wait loop. Show the would-be deployment command labeled with its blocked prerequisites; printing it is not a claim that dispatch is permitted.

   **Explicit override:** `--allow-red-main` skips waiting for a known pending/non-success/absent run and prints an unsuppressible warning naming the SHA, observed state and URL (or no-run result). It never turns an API error into a known CI state and never bypasses tenant/auth/environment checks. Preserve this override only when the caller supplied it; a timeout does not enable it automatically.
4. **Tenant configuration and Environment exist.** Verify the current repository/tenant configuration agrees with the map and resolves the tenant's actual served endpoint and deployment parameter set. A remembered domain, guessed/default URL or absent target evidence is a halt before dispatch; a valid configured tenant is the positive control. Then verify the Environment exists. Confirm the resolved `env` is a real Environment:
   `gh api "repos/$repo/environments" --jq '.environments[].name'` must include `env`. If not, halt:
   `Environment '<env>' does not exist; this skill does not bootstrap new tenants.`
5. **In-flight check (warn, do not block).** `gh run list --workflow=infra.yml --event=workflow_dispatch --json status,databaseId --jq '.[] | select(.status=="in_progress" or .status=="queued") | .databaseId'`.
   If a dispatch run is already active, surface it. The workflow serializes per-Environment via its
   `concurrency` group (`cancel-in-progress: false`), so a new dispatch queues behind the active one
   rather than racing — note that, and let the caller decide whether to proceed or wait.

### Step 2 — Dispatch (fire on invocation)

**Dry-run:** print the resolved `env`, the `origin/main` SHA + subject, and the exact command that
*would* run, then stop. Do not dispatch.

**Revalidate immediately before dispatch.** Fetch `origin/main` again and compare its full SHA with the one checked. If it changed, report the new SHA/subject and repeat Step 1.3 for that revision within the original wait deadline. Do not dispatch a new main using an older SHA's green run. Revalidate the exact-SHA newest CI run/attempt even when main is unchanged. If the deadline has elapsed, report pending and stop; do not dispatch. An explicit `--allow-red-main` still requires a fresh known-state warning for the new SHA.

Then record the dispatch wall-clock time and dispatch against committed main:

```
dispatched_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
gh -R "$repo" workflow run infra.yml -f environment="$env" --ref main
```

`-f environment=<env>` supplies the `environment`-typed `workflow_dispatch` input; `--ref main`
deploys the committed default branch.

### Step 3 — Resolve the run id

`gh workflow run` may return no run id. Record the pre-dispatch run inventory,
checked full SHA, tenant Environment, workflow identity and dispatch timestamp.
Poll the exact repository/workflow/event/branch after dispatch for at most 60 seconds.
Inspect candidate run details, actual `headSha`, event, URL and tenant input/job
Environment; a timestamp or newest-run ordering alone is not identity proof.
Require one uniquely attributable newly created run matching this dispatch.
Concurrent candidates, unavailable input evidence or lookup failure leave run identity
unknown: report the dispatch receipt and candidates, do not dispatch again.

Compare the identified run's actual full `headSha` with the checked SHA immediately.
A mismatch is `DISPATCH_REVISION_MISMATCH`: surface it at once, retain the actual
run/tenant/SHA evidence, and never claim that revision passed the prerequisite gate.
Do not retry dispatch or cancel the remote run automatically. Read-only monitoring
may establish its eventual outcome, clearly separated from checked-revision success.

### Step 4 — Monitor to completion

Launch a **silent** polling loop through the available Codex execution/session tools described
in Step 1.3, mirroring the `/merge` wait-loop discipline: zero stdout on non-terminal ticks, exactly one terminal line. Poll
`gh run view "$RUN_ID" --json status,conclusion` every ~30s. The deploy job's own timeout is 45 min;
add build time, so set the loop ceiling to ~50 min (3000s).

On every poll retain and verify the run id, actual full SHA and event. Known
nonterminal states wait within the original monitor deadline; completed states
report their exact conclusion. Retry transient read/parse failures at most three
consecutive times; authentication errors, identity mismatch and unknown status halt
with `DEPLOY_LOOKUP_FAILED`. Do not convert a failed read to a timeout or success.
A completed successful run is reported as checked-revision success only when the
actual SHA matches the gate receipt. Inspect job/step outcomes and preflight output
before claiming each layer ran; a skipped preflight is not proof of deployment.

Terminal results include `DONE success`, `DONE failure`, `DONE cancelled`,
`TIMEOUT`, `DEPLOY_LOOKUP_FAILED` and `DISPATCH_REVISION_MISMATCH`. Keep individual
waits at most 60 seconds and report only meaningful changes. Stopping a local watcher
does not cancel the remote deployment.

### Step 5 — Report

- **`DONE success`** — report: ✅ deploy succeeded, the run URL, and that the preflight gate passed
  (cite the observed preflight step result and its per-layer report). For `cor.org`, note the live-proof signal the
  preflight asserts: SAGE loads its seeded vault with no `OperationalError`.
- **`DONE failure`** — identify the failing job and step from
  `gh -R "$repo" run view "$RUN_ID" --json jobs` (find the job/step whose `conclusion == "failure"`),
  and tail the failing logs: `gh -R "$repo" run view "$RUN_ID" --log-failed`. Surface the failing
  step by name and the relevant log tail. The preflight step (`Post-deploy preflight gate`) reports
  every layer at once with anti-coincidental controls — if it is the failing step, surface its
  per-layer report so the caller sees all broken layers, not just the first. Always include the run
  URL so the caller can drill in.
- **`DONE cancelled`** — report the run was cancelled (likely a superseding dispatch or a manual
  cancel) with the run URL.
- **`TIMEOUT`** — the loop ceiling elapsed; CI may still be running. Report the run URL and instruct
  the caller to check the run or re-invoke `/deploy` once it settles. Do not dispatch again.

End with a compact status block:

```
STATUS: <succeeded | failed | cancelled | timed_out | unknown | revision_mismatch>
TENANT: <domain> (<env>)
CHECKED_COMMIT: <full-sha> <subject>
ACTUAL_COMMIT: <full-sha or unknown>
RUN: <run url>
```

## Out of scope

- **First-time tenant bring-up.** The irreducible manual floor (Discipline §"The irreducible manual
  floor") — the one-time CI deploy-identity bootstrap, procuring a domain + wildcard TLS cert,
  publishing the DNS records, and creating the tenant's GitHub Environment + parameter set — is
  manual and out of scope. The skill halts if the Environment does not already exist.
- **DNS publication.** DNS is a manual, provider-agnostic step (Discipline §"DNS is a manual …");
  the deploy emits the records to publish but never scripts a provider.
- **Editing infra or application code.** Changes to `infra/`, `deploy/`, or app code land through
  `/kickoff → /commit → /merge`; `/deploy` only ships what is already on `origin/main`.
- **Rollback.** Re-deploying an earlier commit is a fresh `/deploy` after that commit is on `main`;
  this skill has no rollback verb.
- **Non-cas repos.** The skill is CAS-specific (the domain→Environment map and `infra.yml` shape).

## Notes

- **Why fire-on-invocation.** Per the caller's standing choice, `/deploy <domain>` is itself the
  authorization to dispatch — analogous to `/commit` authorizing the push. The prechecks (Step 1)
  are the guardrail; `--dry-run` is the look-before-you-leap path when wanted.
- **Always deploys `origin/main`.** The dispatch targets `--ref main`; the local working tree is
  irrelevant to what ships (Principle 1). Report the SHA so the operator sees the artifact.
- **Per-tenant serialization.** `infra.yml`'s `concurrency` group is keyed on the Environment with
  `cancel-in-progress: false`, so two `/deploy cor.org` invocations queue rather than race or cancel.
- **Dispatch correlation.** Follow Step 3; timestamps discover candidates but never establish identity by themselves.
- **Adding a tenant.** Add a row to the domain→Environment map. The Environment itself, its
  federated deploy identity, and its parameter set are provisioned out of band (the manual floor).
- **Green-main precheck (v0.2.0).** The ruleset's strict up-to-date policy came off, so `main` can
  be transiently red where it previously could not: a pull request may land having been tested
  against an older `main`, and the `push` run after the squash is the first to see the merged tree.
  This skill dispatches `origin/main` HEAD and used to inherit a green `main` for free; Step 1 now
  reads that commit's CI conclusion itself. `--allow-red-main` keeps an infrastructure-only deploy
  possible during a red suite, at the cost of an unsuppressible warning.

- **Prerequisite CI wait (v0.3.0).** Pending exact-SHA push CI now waits instead of requiring repeated deploy invocations. Discovery has a 120-second grace period and the total wait has a 3000-second ceiling. Non-success terminal states and failed lookups block deployment; dry-run reports immediately. Main and the newest CI attempt are revalidated before dispatch. The existing explicit override is preserved.
- **Branch-ref limitation.** Dispatch still uses `--ref main`, so an update between the final check and GitHub accepting the dispatch can race it. Resolve the deployment run's actual `headSha` and compare it with the checked SHA before reporting which revision shipped. If they differ, surface the mismatch immediately; do not claim the deployed revision passed this gate or dispatch again. Eliminating that last race requires a workflow-level immutable-revision contract, outside this wait-loop change.
