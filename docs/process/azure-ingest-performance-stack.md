# Isolated Azure ingestion performance stack

This manual experiment supports T-0761. It measures the immutable PR #736 image
in a separate resource group, network, Container Apps environment, database,
managed identities and SharePoint root. It does not modify production capacity.
The existing [measurement procedure](ingest-performance.md) remains the
interpretation authority. The workflow is `ingest-performance.yml`.

## Preconditions and approval

Merge this implementation separately before using the workflow on `main`.
No workflow dispatch or successful local check establishes deployment acceptance.
Review the exact specification bytes and retain their SHA-256. Never include a
credential in the specification. The controller checks the current tenant,
subscription, reference configuration, candidate image availability, successful
candidate CI, regional profiles, database SKU, quotas and retail rates before
creating resources. Any unavailable prerequisite stops the run.

The production deployment identity's standing grants are intentionally unchanged.
A tenant operator must separately establish the experiment operator's permission
to deploy into the two exact trial groups, assign the declared trial roles there,
and assign AcrPull on the source registry and Secrets User on the exact
`anthropic-api-key` secret. Register Microsoft.Automation and Microsoft.Storage
alongside the production template providers. Do not give the expiry identity
subscription-wide rights: it receives Contributor on the exact disposable trial
resource group only. The runtime identity receives Sites.Selected and write
permission on the explicitly selected SharePoint site.

The current automation additionally requires its operator to have Graph
AppRoleAssignment.ReadWrite.All and Sites.FullControl.All to grant and revoke
that new runtime identity. These are directory/site administration capabilities,
not implied by Azure Owner or the existing deployment identity. The controller
checks for them and refuses to provision when absent. This implementation never
assigns those operator privileges. Tenant approval and a suitably authorized
operator remain deployment prerequisites; do not broaden the production identity
merely to make the experiment pass. Its data-plane token must also be accepted
by the existing SAGE audience. Read these capabilities back before dispatch.

## Specification

The JSON object has `schema_version: 1`, a fresh lowercase `run_id` (3–20
characters, letters/digits/hyphens), `subscription_id`, and `location`.
Do not provide runtime `created_at`/`expires_at`; the controller derives them once.

- `evidence_public_key_pem`: an owner-controlled RSA public key of at least
  3072 bits. Keep its private key outside the repository and CI.
- `source`: the serving reference `resource_group` and `app`.
- `candidate`: exact 40-character merged `commit`, immutable `image` in the
  existing registry (`registry.azurecr.io/sage@sha256:…`), and
  `abstraction_model` matching the current serving configuration.
- `binding`: explicit `site_id`, `drive_id`, `audience`, `tenant_id`, `registry_id`
  and `key_vault_id`. Both resource IDs must belong to the selected reference
  group in the exact subscription.
- `limits`: `window_seconds: 14400`, `per_run_seconds: 900`,
  `incremental_usd: 50`, `provider_calls: 30` (lower limits are accepted).
- `arms`, in order:
  `[{"profile":"Consumption","cpu":2,"memory":"4Gi"},
  {"profile":"D8","cpu":2,"memory":"8Gi"},
  {"profile":"D8","cpu":4,"memory":"8Gi"}]`.

The controller derives `rg-ingest-perf-<run>`,
`rg-ingest-perf-control-<run>`, `performance/<run>` and `perf_<run>` (hyphens
become underscores for PostgreSQL schema identifiers). Each service arm creates
a fresh vault through SAGE. Run directories and populated vaults are never
blindly replayed. Repeated infrastructure preparation may resume only with the
same original clock, matching local receipts and fully owned live inventories,
before the service or source root has been activated.

## Execution and evidence

Store the exact reviewed JSON in the selected environment secret
`PERF_APPROVED_SPEC_JSON`. Dispatch the committed manual workflow with that
environment and `approved_sha256`. Binding coordinates are not public dispatch
inputs. Resource mutation is refused outside that
repository's manual `main` workflow. The implementation first creates the small
evidence/cleanup foundation, then separately previews and assigns access using
its now-known identity IDs. Every create phase requires a retained, matching
ARM validation and what-if inventory; foreign, destructive or extra mutations
are refused. It publishes and reads back the exact expiry runbook,
runs its real cloud ownership inspection, and reads back the schedule. Only
then may the billable D8/PG17 stack be applied. Database bootstrap uses the
canonical bootstrap module and immutable baseline image. The trial changes
backup retention to seven days and omits deletion protection; production callers
retain 35-day retention and deletion protection by default.

Microbenchmarks run serially in fresh processes on the dedicated D8 node before
the service exists. They compare actual batches 1/2/4/8, short/long/mixed inputs,
Torch thread counts and the existing fair singleton dispatch. The 2/4 CPU D8
comparison holds memory at 8 GiB. A pilot failure stops the experiment. The
controller records pruning instead of claiming a completed matrix if the
remaining window cannot accommodate service trials and export.

The service wrapper is separately reviewed code mounted beside the unchanged
image. Its script hashes and runtime controls are pinned in the trial manifest
and checked against Azure before writes and after restarts. It adds five-second
process/cgroup samples, durable trace export, one-shot exec-only fault controls,
and a durable compare-and-swap provider budget. It disables SDK retries and
limits output to the serving configuration's 1,500-token ceiling. This execution
difference must accompany all baseline comparisons. Every SDK HTTP attempt
reserves one of 30 calls before sending, including metadata calls, count-token
requests and retries; cumulative submitted bodies are bounded to 10 MB.

The per-run limit bounds each benchmark container, each service arm including
its restart, and each fault/recovery control including request and restart time.
Azure job replica timeouts and the child/caller deadlines receive the same limit.
Provisioning and readiness are separate setup phases inside the overall expiry.

Each service arm checks health, search and reads during ingestion, retains
latency samples and Azure metric series, and verifies source bytes through SAGE.
The timing-overhead service pair disables abstraction in both arms to preserve
the shared provider budget. Fault controls require actual nonce-correlated
worker/provider receipts and final recovery states. A separate read-only job
checks every source identity, passage identity/order/content, normalized vector
and independently re-encoded passage/document-surface association. Reference
encoding is outside timed intervals.

Private Blob evidence survives service restarts and CI loss. This repository is
public, so GitHub artifacts contain only encrypted archives and public ciphertext
hash receipts. Each archive uses a fresh AES-256-GCM key, wrapped to the approved
owner RSA public key with OAEP/SHA-256. Raw evidence and binding coordinates are
never uploaded as public artifacts. Preserve the private key locally; its loss
prevents artifact recovery. Authenticate and decrypt a downloaded archive into
a new private directory using:

```sh
.venv/bin/python -m deploy.performance_archive decrypt --source evidence.enc \
  --private-key /private/path/recipient.pem --output /private/path/recovered-evidence
```

The archive must authenticate completely before extraction. Failure to seal or
upload the pre-cleanup archive blocks controller cleanup, preserving private
Blob evidence for recovery. Each export retains raw records and a hash manifest. `report.md` and `report.json` distinguish missing
evidence, overlapping stage totals and hosting/resource differences. Reported
CPU samples and one-minute Azure metric series must be correlated by their actual
intervals; an absent series is unknown. No throughput result is present in this
PR, and local tests cannot establish Azure provisioning or performance success.

## Cleanup and failure recovery

The workflow performs a fresh export and archives complete or partial evidence
before deletion. Before remote work, export independently attempts to mark the
state as started and remove the previous receipt; either invalidated channel
blocks use of old proof. If both channels are unwritable, durable revocation is
impossible: repair local storage, then run a fresh successful export and archive
before any recovery cleanup. Never reuse an old digest after such a failure.
The fresh export step must succeed independently of the measurement outcome. Cleanup
requires its exact receipt SHA-256, matching attempt/run/plan/export target and
unchanged exported file hashes. An earlier successful export cannot authorize
cleanup after a later failed attempt. Export after expiry remains available
through the original reviewed specification and runtime-plan binding. Cleanup
checks exact group IDs, all resource ownership/expiry tags, managed identities,
site grants and external role assignments. It removes only the recorded trial
access, deletes the disposable group, observes absence, then deletes the evidence
controller group. Confirmed-absent assignments/groups are safe to repeat. Unknown
inventory or export status retains the controller and fails the run.

At expiry, the independent Automation identity checks all ownership again and
deletes only its exact trial group, even if CI has disappeared. It retains the
controller/evidence group; an operator must export that evidence, revoke recorded
external access and finish controller cleanup. If initial foundation setup fails
before a schedule is proven, no billable stack is started; inspect its receipts
and remove only the owned foundation after exporting the available evidence.
A subscription budget is not a hard spending stop. The bounded allocation,
request/input limits, conservative rate estimate and independently scheduled
expiry are the controls; preserve their readbacks and actual cleanup outcome.
