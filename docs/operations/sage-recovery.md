# SAGE recovery verification

These tools observe an explicitly selected hosted SAGE deployment, reconcile six
retained CSV sources, and verify a single controlled restart followed by a real
30-minute stability window. They preserve the original sixteen-document backlog
as the acceptance population. They do not upload, delete, replay or recompute
vault records. A successful test run or repository merge is not live recovery
acceptance.

Run from the repository root with its locked `.venv`. Invoking the package without
a command, requesting help, or importing a module performs no service operations:

```sh
.venv/bin/python -m scripts.sage_recovery --help
```

## Private configuration and evidence

Keep configuration, tokens, source manifests, original reconciliation ledger, CSV
content and runtime evidence outside all Git checkouts. Create owner-private work
and export directories before use. Keep those two directory trees disjoint. The
operator configuration, token cache, manifest, ledger and original CSV paths must
be outside both output trees. Configuration, token, manifest and ledger files
must also have distinct paths. Input/output overlap
is refused before dispatch. The vault configuration snapshot is named
`vault-config.json`. Use the same work directory across controller
starts so native-observation pacing, returned Azure backoff and restart intent
survive. Do not remove or relocate guard markers to force a retry.

Supply a private JSON configuration with exactly these fields:

| Field | Meaning |
|---|---|
| `base_url` | Verified HTTPS origin, without a trailing slash or path |
| `audience` | Existing authorized Entra resource audience, without `/.default` |
| `subscription`, `resource_group`, `app` | Explicit Azure target |
| `revision`, `image` | Exact approved revision and image pins |
| `vault_id` | Vault resolved from the deployment's live inventory/configuration |
| `indexed_document` | Known indexed positive-control document |
| `expected_ids` | Sixteen unique original backlog document IDs |
| `six_ids` | Six unique CSV document IDs belonging to that backlog |
| `root`, `evidence_out` | Absolute private state and durable export directories |
| `token_file` | Existing private Azure token-cache JSON file |
| `manifest`, `ledger` | Absolute paths to original source/projection manifest and reconciliation ledger |
| `manifest_sha256`, `ledger_sha256` | Previously approved SHA-256 pins of those exact original files |

The configuration contains no execution-consent field. Control consent is supplied
only on the command line. Missing/unknown configuration fields, mismatched original
file digests, invalid target names and paths inside Git are refused before command
dispatch. Config validation does not prove that an operator selected the right
subscription or vault: verify the target and its served contract before running.
The tool reads the existing token cache and uses the existing Azure sign-in to
refresh it when necessary; it does not initiate interactive sign-in or new grants.
Token refresh is coordinated across threads/processes and published atomically.
The effective configuration has a stable fingerprint excluding transient execution
consent. Controller child commands must match that pinned fingerprint before any
external request. Observer runs, cycles, requests and verifier artifacts carry it;
acceptance and progress/export consumers reject mismatched configuration evidence.
Keep the cache owner-private; never put its contents into a PR or terminal report.

The manifest is a six-element JSON array. Each entry has `filename`, `source`
(local CSV path, needed only for `inspect-inputs`), `bytes`, `sha256`,
`expected_projection_sha256`, and three `samples` covering early/middle/late
records. Each sample has `identity` and `row`. The ledger is a six-element JSON
array with matching `filename`, `document_id`, and original `metadata` objects.
The verifiers require unique filenames/identities, exact retained bytes and
projection digests, preserved metadata/provenance, one-member linear lineages,
positive sampled keyword/semantic retrieval and successful terminal pipelines.
A differing or longer legitimate pre-existing lineage needs explicit reconciliation
and a reviewed verifier change; these tools will refuse it.

Do not replace the approved digest pins with hashes of a changed file to silence a
failure. Preserve the original evidence and resolve the discrepancy.

## Read-only commands

Use an absolute private configuration path in place of `/secure/recovery/config.json`:

```sh
.venv/bin/python -m scripts.sage_recovery --config /secure/recovery/config.json snapshot
.venv/bin/python -m scripts.sage_recovery --config /secure/recovery/config.json progress
.venv/bin/python -m scripts.sage_recovery --config /secure/recovery/config.json monitor
.venv/bin/python -m scripts.sage_recovery --config /secure/recovery/config.json verify-sources --run-id CURRENT_OBSERVER_RUN
.venv/bin/python -m scripts.sage_recovery --config /secure/recovery/config.json verify-projections --run-id CURRENT_OBSERVER_RUN
.venv/bin/python -m scripts.sage_recovery --config /secure/recovery/config.json metrics --start-time 2026-01-01T00:00:00Z
.venv/bin/python -m scripts.sage_recovery --config /secure/recovery/config.json inspect-inputs
.venv/bin/python -m scripts.sage_recovery --config /secure/recovery/config.json export
```

`monitor` is a foreground observer. Its six probes cover health, inventory, direct
read, catalog, positive keyword search and positive semantic search. Before starting,
put the exact approved `expected_ids` array in `root/backlog-ids.json`. One process
owns its file lock. A `root/stop` marker ends its loop; an existing marker requires
manual reconciliation rather than deletion. Stopping the observer alone leaves
SAGE active. Starting an observer is not authorization to run the controller.

`progress` reports only fresh, matching-run observations. `export` copies an explicit
allowlist of saved evidence and makes no live acceptance claim. Azure minute metrics
can miss short peaks; absent datapoints do not prove zero restarts or memory usage.
`inspect-inputs` loads the locally cached pinned tokenizer and actual source adapter
and chunker, without inference. It verifies reconstruction and token budgets; it
is not a measurement of production model throughput or memory.

## Guarded production control

Only an explicitly authorized operator may invoke:

```sh
.venv/bin/python -m scripts.sage_recovery --config /secure/recovery/config.json control --execute
```

This command can restart or deactivate the pinned Azure revision. It requires an
active foreground observer and exact revision/image, one ready unrestarted replica,
2 CPU / 4 GiB allocation, actual native memory evidence and the original backlog.
It waits for all sixteen records to reach successful `abstraction_complete`, verifies
the six sources/projections, writes restart intent before sending one restart, and
requires a ready replacement with fresh successful probes before starting the window.

All periodic and mandatory native memory reads share a persisted app/subscription
budget and an exclusive lock. The baseline spacing is fifteen minutes; a returned
Retry-After extends eligibility. This interval is conservative, not a documented
Azure quota guarantee. Eligible same-run/same-replica measurements can be reused only
with their original timestamps and appropriate boundary freshness. Measured RSS
must be at most 3 GiB, cgroup peak below 4 GiB, memory-limit failures zero and CPU
quota two. Native evidence currently supports the cgroup-v1 paths used by the
selected deployment; a different layout pauses observation rather than passing it.

A diagnostic transport failure, a non-successful backlog-status response, repeated
throttling, or mismatched configuration evidence pauses the local controller,
saves `verification-paused.json`, and does not deactivate production or declare
success. A confirmed guarded safety failure uses the pinned revision's deactivation
path and records whether deactivation was confirmed. Resource/replica/observer drift
is also a safety refusal. Diagnose every stop; do not blindly resume.

If a restart command times out or the process exits after intent publication,
reconcile Azure state and the saved intent manually. Existing `restart-issued.json`,
`window.json` or `stop` markers refuse a new control run. Missing evidence never
means that a mutation did not happen. The tool does not automatically replay it.

Acceptance requires thirty continuous wall-clock minutes with exact replica identity,
complete fresh six-probe cycles, no failed/over-five-second request, lightweight p95
at most two seconds, and final source/projection and native-memory verification.
The exporter cannot grant acceptance. Retain evidence outside the checkout for owner
review; ticket closure, deployment and unattended scheduling remain separate decisions.
