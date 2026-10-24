# Isolated Azure ingestion performance

The manual `ingest-performance.yml` workflow measures an immutable workload image
using a separately committed experiment controller. The [measurement procedure](ingest-performance.md)
is the interpretation authority. Source readiness, merge, operational approval,
Azure execution and measurement acceptance are separate checkpoints.

## Staged operation

First retain existing production stage summaries and aligned application/database
metrics read-only. Label them passive observations of their actual builds and
workload mix. They are not the controlled baseline and authorize no new ingestion.

Next prove independent expiry with an **embedding-only foundation rehearsal**.
Use `phase: embedding_canary`, `expiry_only: true`, a fresh run identity, a short
window and the reviewed immutable baseline. Bootstrap creates only empty owned
groups and scoped grants. The workflow creates identities, private evidence
storage and Automation; publishes and reads back the committed cleanup script;
executes its ownership inspection; and enables the exact independent schedule.
It exits without creating a Container Apps environment or starting a model.
The workflow retains the controller and does not perform immediate cleanup for
this rehearsal. Wait for the independent schedule to delete the trial group.

Read back the completed Automation job, its exact `Expire` parameters, published
script bytes, actual `deleted` output and trial-group absence. A successful
`Inspect` job is insufficient. Retain that private evidence before approving the
next specification. Keep the rehearsal controller until the embedding run has
verified this evidence; then finish its separately authorized cleanup.

The first measurement uses another fresh `phase: embedding_canary` run and an
`expiry_proof` naming that rehearsal's `run_id`, `expires_at` and concrete `job_id`.
The controller verifies it live, including the committed runbook and actual
deletion readback, before creating compute. The proof must be from the preceding
seven days and cannot name the current run. It grants the measurement operator
read access to that exact prior Automation account for this verification.

The measurement stack contains one Consumption environment and one manual job,
with one container at **2 CPU / 4 GiB**, zero retries and the reviewed per-run
timeout. A probe runs the mounted wrapper without importing/loading the embedding
model. The writer managed identity stores a fresh nonce; the separate authenticated
recovery reader retrieves and compares it. Model startup is refused unless that
proof is current. The controller restores and reads back the full job template,
then starts one short-input, singleton, two-thread case. The existing benchmark
performs one warmup and three measured repetitions against the real pinned model,
including token accounting and vector count/order/normalization checks.

Every job start persists a plan-bound intent before the remote request. A lost
response, denied execution read or invalid returned identity retains that intent
and refuses redispatch. A retained dispatch also refuses infrastructure replay,
so recovery cannot overwrite the evidence-bearing provision receipt. Every export
entry revokes old cleanup proof before validating retained inputs; historical
downgraded receipts with dispatch attempts still require cloud evidence.
Export and cleanup use the original retained plan and
clock; an intent-only attempt is evidence-bearing, never an empty foundation.

Stop after this case, encrypted evidence export/readback and verified cleanup.
A singleton-versus-true-batching comparison is a later explicitly approved
expansion. The full service path, fixed-memory CPU comparison, larger matrix,
provider-enabled sources and contention/recovery campaign remain later work;
they are not dependencies of the first embedding canary. Do not start dedicated
capacity merely to establish this small case.

## Specification and limits

Use JSON with `schema_version: 1`, `phase: embedding_canary`, a fresh lowercase
3–20 character `run_id`, `subscription_id`, `location`, and:

- `evidence_public_key_pem`: owner-controlled RSA public key, at least 3072 bits.
  Keep the private key outside the repository and CI.
- `source.resource_group`: the exact existing registry group.
- `binding.registry_id` and `binding.tenant_id`: exact immutable image access and
  tenant coordinates. No source application, Key Vault, SharePoint, database,
  abstraction provider, SAGE audience or Graph administrative grant is required.
- `candidate.commit`: `eb994495b66837cca23cc891487f83ad434c0c4f`;
  `candidate.image`: `acrwofzhg37aohom.azurecr.io/sage@sha256:feaa6269b1244467b7d03d7bfa371209fa7c88e73569f36a2b8ec469291cec2c`.
  Record the controller commit independently; later production changes do not
  silently replace this measurement baseline.
- `arms`: `[{"profile":"Consumption","cpu":2,"memory":"4Gi"}]`.
- `limits`: at most `window_seconds: 3600`, `per_run_seconds: 900`,
  `incremental_usd: 5`, `provider_calls: 1`. The legacy field is retained but no
  abstraction provider is used. Prefer a much shorter expiry-only rehearsal.
- `expiry_only: true` for the independent deletion rehearsal; otherwise provide
  the private `expiry_proof` described above.

Do not provide runtime timestamps. The bootstrap derives them once, and handoff
preserves the original clock. The one-hour/$5 values are source/runtime ceilings
and conservative planning allowances, **not live spending authorization or a hard
Azure budget**. Read current regional quota and retail rates before dispatch;
the estimate includes bounded controller/storage/probe allowance. Unknown quota,
missing meters or estimates above the reviewed limit halt the measurement.

Derived groups are `rg-ingest-perf-<run>` and
`rg-ingest-perf-control-<run>`. Identities, storage paths, ownership tags and
expiry remain bound to the exact run. No unaudited production resource is reused
for load; the production registry supplies only the immutable image.

## Scoped operator bootstrap and handoff

Use the separate experiment application/environment with one exact GitHub
`repo:clifguy/cas:environment:<environment>` federated subject and a main-only
branch policy. From merged reviewed code, a separately authorized operator runs:

```sh
.venv/bin/python -m deploy.performance_bootstrap bootstrap \
  --spec /private/path/spec.json --approved-sha256 SPEC_SHA256 \
  --environment EXPERIMENT_ENVIRONMENT --principal EXPERIMENT_SP_OBJECT_ID \
  --output /private/path/bootstrap
```

The embedding path retains one narrowly defined orchestration role: deployment
read/write/validate/what-if/delete and read-only Container Apps regional usage,
subscription, resource-group and role-assignment/definition inspection. It is assigned at the subscription
and exact source group for nested deployments. This is not subscription
Contributor/Owner or subscription role administration. Each trial group grants
Contributor and conditioned RBAC administration: Contributor assignments in the
trial group, Blob Data Contributor in the controller group. The source registry
grants Reader/AcrPull and conditioned AcrPull assignment. The conditions restrict
role and ServicePrincipal type, not arbitrary PrincipalId; the separate fail-closed
ARM preview binds actual runtime, expiry and recovery-reader principals to exact
receipt-backed UUIDs. An extra scope, privilege, principal or altered condition
is refused on live readback. No tenant-wide Graph administration is required.

Intent is written before every bootstrap mutation. Ready is written only after
exact group, role-definition and assignment readbacks. A successful repeat verifies
the same empty inventory without renewing the clock. An interrupted intent is
refused; reconcile its recorded effects before an explicitly authorized recovery.
Do not manufacture ready receipts or silently normalize unknown Azure responses
to absence. Only recognized not-found establishes absence.

Keep exact specification and ready receipt bytes in the selected environment's
`PERF_APPROVED_SPEC_JSON` and `PERF_BOOTSTRAP_RECEIPT_JSON` secrets. Dispatch with
both SHA-256 values. Preserve terminal newlines when uploading secrets: the
GitHub CLI stdin path and shell substitution can trim them. Use the reviewed
file bytes as an explicit process argument from a trusted machine without tracing.
The workflow checks both hashes, environment and authenticated principal, and
materializes the original runtime plan. Setup time consumes the original window.

Every deployment phase runs actual ARM validation and FullResourcePayloads
what-if before apply. Access previews use concrete principals from foundation
outputs; unresolved expressions and Unsupported changes remain refused. Foreign
scopes, extra resources, dedicated capacity and unexpected deletions are refused.
No local fixture or template compile establishes live authorization/startup.

## Evidence, failure recovery and cleanup

Private Blob evidence survives job/controller-process failure. Partial records
are retained. Public GitHub artifacts contain only encrypted archives and their
ciphertext receipts: fresh AES-256-GCM keys wrapped with owner RSA OAEP/SHA-256.
After authenticated download, decrypt and read back the expected files privately:

```sh
.venv/bin/python -m deploy.performance_archive decrypt --source evidence.enc \
  --private-key /private/path/recipient.pem --output /private/path/recovered-evidence
```

Authentication completes before extraction. Retain source/image/model/controller
identity, warmup/repetitions, submitted token counts, peak process/cgroup memory,
execution status, probe nonce, export hashes and actual cleanup readbacks. Missing
results are partial/unknown. Later service/source acceptance remains unresolved.

For an authenticated premeasurement foundation with no dispatched job and no
compute resource, export may retain a live ownership/inventory snapshot. It does
not demand nonexistent model evidence. A partial compute deployment or any
dispatch requires durable cloud export; denied/interrupted export cannot be
relabelled empty. Empty-foundation cleanup checks its live inventory again.

Every running experiment requires a fresh complete export, encrypted archive and
its exact export-receipt SHA-256 before immediate cleanup. Starting a later export
invalidates old proof; missing/changed files or interrupted export block deletion.
Cleanup verifies ownership, expiry, identities and scoped grants, deletes trial
resources and observes absence before removing the controller. The embedding path
retains an explicit `operator-grants-pending.json` because CI cannot revoke its
own subscription/source bootstrap grants. The authorized human operator then runs
the same bootstrap entry point with `cleanup`, the original `--receipt`,
`--approved-bootstrap-sha256`, specification digest, environment and principal;
it requires resource-group absence, revokes only exact matching assignments and
reads their absence back. Resource cleanup success is not access cleanup success;
custom orchestration role definitions remain only as non-billable shared definitions.
Remove definitions only after independently proving no other assignment uses them.

Independent expiry deletes only the owned trial group and retains private evidence
in the controller group. After expiry or failed CI, export retained evidence,
finish exact recorded grant/controller cleanup and verify actual absence. Rehearsal
controller cleanup follows its own original receipts. Disabling experiment
federation/application/environment and removing handoff secrets are separate
operator cleanup steps; retain their readbacks. Never claim those occurred merely
because resource-group deletion passed. On structural integration failure, stop
expansion and reassess the whole path without weakening preview or evidence guards.

The existing `service_campaign` path is retained for later separately approved
work. It still has source/database/provider prerequisites and the larger matrix;
its existence does not authorize running it or make it a prerequisite for the
embedding canary.
