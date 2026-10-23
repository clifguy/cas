# Measuring Azure ingestion performance

Azure measurements are the acceptance evidence for deployed-service performance.
Local timings support correctness and preliminary experiments only. Instrumentation
preserves the production model, singleton scheduling quantum, fair admission,
projection, passage content and transactional write behavior.

## Stage records

With the vault's existing `timing.enabled` setting enabled, the
`sage.ingestion.performance` logger emits one JSON summary per operation to the
normal application log. This makes the record available in Container Apps console
logs. It does not add a persistent telemetry table or alter API responses. The
records carry vault/document identity, an operation ID, UTC start/finish timestamps,
monotonic stage boundaries, outcome, aggregate counts and token-length ranges.
Queued pipeline work establishes a fresh context when its worker starts; its
queue wait is measured from the job's creation. Interactive retrieval establishes
its own context so embedding waits can be compared with bulk work.

| Stage | What is measured |
| --- | --- |
| `pipeline.queue` | Enqueued job creation through processing entry |
| `projection.queue/execute` | Executor entry wait and projection work |
| `passages.queue/execute` | Executor entry wait and passage construction/sizing |
| `sizing.tokenizer_lock/tokenize` | Lock wait and actual sizing tokenization |
| `embedding.admission` | Fair admission semaphore wait |
| `embedding.executor` | Submitted job through actual worker entry |
| `embedding.tokenizer_lock` | Shared tokenizer lock wait |
| `embedding.tokenize` | Actual model preprocessing and its attention-mask token counts |
| `embedding.encode` | Inclusive encode call, including preprocessing and vector conversion inside the model |
| `database.pool/begin/write_chunks/commit/rollback` | Content-store pool acquisition, transaction entry, passage writes and actual transaction exit |
| `graph.pool/begin/commit/rollback` | Graph-store pool acquisition and explicit transaction boundaries |
| `abstraction.prepare` | Model-limit discovery and input fitting, including token-count API calls |
| `abstraction.provider_request` | Hosted SDK request, including network/provider wait and SDK-internal retries |
| `abstraction.retry_backoff` | Service retry delay |
| `pipeline.attempt` | One complete service attempt; count distinguishes retries |

Stage totals **overlap**. In particular encode includes preprocessing, pipeline
attempts include sub-stages, and a process can run different workers concurrently.
Do not sum stages to reconstruct elapsed time. Monotonic values are comparable
within a process; UTC timestamps join Azure metric intervals across processes.
Transaction exit success is recorded only after the real exit returns; commit
failure is recorded as failure. Status counters distinguish failed pipelines even
when the service returns its document rather than raising.

Token totals come from actual preprocessing attention masks, after the model's
configured truncation and excluding padding; they are not an estimate from bytes.
Sizing is a different operation. Token length minimum/maximum and aggregate totals
are bounded observations, not a retained per-passage histogram. Missing values are
unknown. Provider input/output usage is recorded when returned. Server-side prefill,
decode and SDK-internal retry counts are not inferred from request wall time.

Each operation retains at most 32 named stage/count/range keys plus an overflow
bucket, rather than a record per passage. No text, paths, exception messages or
credentials are included. A cancelled request's summary waits for already-submitted
worker work to drain; its outcome remains cancelled. The existing timing setting
can disable these observations. Measure enabled/disabled overhead before accepting
Azure throughput results. Normal console-log retention/ingestion charges still apply.

## Model-call benchmark

Run a fresh process for every CPU/thread/batch/shape arm inside the exact candidate
`linux/amd64` image. Bind `BENCHMARK_IMAGE_DIGEST` to the independently verified
immutable image. Capture the Container Apps revision/replica and Azure resource
settings outside the process too; environment labels alone are not deployment proof.

```sh
python -m sage.utils.embedding_benchmark --output batch-two-short.jsonl \
  --shape short --batch-size 2 --threads 2 --interop-threads 1 \
  --warmups 1 --repeats 3
python -m sage.utils.embedding_benchmark --output fair-short.jsonl \
  --shape short --fair --batch-size 1 --threads 2 --warmups 1 --repeats 3
```

Use shapes `short`, `long` and `mixed`, batches 1/2/4/8 and explicit Torch thread
settings. Prune an unsafe arm using bounded pilots; preserve its partial result.
`--uninstrumented` supplies an overhead control. These are microbenchmarks: direct
batch calls do not model fair interactive scheduling and are not a production
batching implementation. Fair mode invokes the actual public dispatch method.

The report separates model load, independent singleton vector reference generation,
warm-up and measured repetitions. It checks returned count, finiteness, normalization
and association against those reference vectors. It writes records incrementally
and refuses to overwrite previous evidence. A failure record prevents a partial
run being read as a completed summary. Process RSS peak is lifetime-cumulative,
including loading/reference/warm-up; use a fresh process per arm. Cgroup counters
are recorded where available; unavailable metrics stay absent.

## Prepare the Azure trial

Preparation is read-only and performs no provisioning:

```sh
python -m scripts.azure_ingest_trial prepare \
  --source-group SOURCE_GROUP --source-app SOURCE_APP --run-id measured-ingest \
  --output trial-preparation.json
```

The resulting manifest is **incomplete preparation**, not deployment authority.
Before an operational approval, complete the exact resource inventory, immutable
candidate image, private PostgreSQL 17/B2s/32 GiB backend, synthetic source identities,
cloud abstraction model, separate document-store root, identity/networking, log
retention, priced estimate and quota/ARM checks. Proposed limits are four hours,
US$50 incremental spend and a 15-minute deadline per service run; these are not
Azure-enforced financial limits. Provision an independent expiry/cleanup mechanism,
not just cleanup in the caller's finally block. Do not create those resources until
that concrete operational plan has been approved.

Use a separate resource group/environment/app/database and an empty `perf-...`
vault. Tag trial resources with `benchmarkRun` equal to the run ID. Use a separate
`performance/<run-id>` document-store root, with explicit approved site/drive scope.
Match production region, architecture, model pin, one-replica ownership, hosted
abstraction settings and database topology; record all differences. The trial
runner verifies the app identity/image/resources/entrypoint/config mount, reads the
selected mounted config secret in memory, checks its approved SHA-256, and verifies
that config points to the owned PostgreSQL endpoint/database and isolated source
root. The manifest must pin `revision_name` and explicitly approve
`restart_before_trial: true`. The runner requires that revision to be the sole
healthy active revision with all traffic and exactly one ready replica. It checks
the actual revision template as well as the desired app template. After validating
the secret/backend, it restarts only this isolated trial revision and waits up to
ten minutes for a different ready container identity, then rechecks the secret and
bindings. This loads the approved secret rather than trusting a control-plane
update alone. The runner checks the process and bindings again after measurement;
a restart or configuration drift invalidates the result. Freeze concurrent control
changes during the trial. Secret contents never enter evidence. See Microsoft's
[revision/secret guidance](https://learn.microsoft.com/en-us/azure/container-apps/revisions)
and [replica state contract](https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/container-apps-revision-replicas/list-replicas?view=rest-resource-manager-containerapps-2026-01-01).

Consumption compares 2 CPU/4 GiB with 4 CPU/8 GiB, which is a **resource-size**
comparison. It cannot isolate CPU allocation at fixed memory. Prepare a separately
validated Dedicated-profile comparison with equal memory and differing two/four app
CPU allocations; allow headroom for platform reservations, confirm regional quota
and price it before approval. Do not silently substitute a local run or CPU-affinity
limit for that allocation comparison. If unavailable, record the acceptance gap for
an owner decision. Microsoft's [allocation table](https://learn.microsoft.com/en-us/azure/container-apps/containers#vcpu-and-memory-allocation-requirements)
and [workload profiles](https://learn.microsoft.com/en-us/azure/container-apps/workload-profiles-overview)
describe the platform constraints; actual deployment validation is still required.

## Execute an approved service measurement

Only after the separate operational approval, from a caller that can reach the
isolated app and use its authorized credentials:

```sh
python -m scripts.azure_ingest_trial execute --manifest approved-trial.json \
  --sources witness.md narrative.md structured.csv --query 'witness phrase' \
  --output service-trial.jsonl
```

Supply the bearer credential through `SAGE_TRIAL_ACCESS_TOKEN` and, when the direct
app edge requires it, the trial-only ingress key through `SAGE_TRIAL_INGRESS_KEY`.
Never put credentials in a manifest or command argument. Byte uploads carry
their one-time, digest-bound transfer credential and the same-origin ingress key
when required, exclude bearer authorization/cookies, and refuse redirects. The
first source is a small positive keyword/semantic witness;
subsequent sources create the measured request load. Use dense quoted JSON/multiline
cells for the structured source. Retain exact source hashes and byte counts. The
runner refuses an already-populated vault rather than replaying uncertain writes.

Require at least 30 positive samples per probe class during ingest requests. This
is **request overlap**, not independently proven overlap with inference: align stage
records and bulk progress before claiming contention evidence. The runner validates
terminal pipeline status and source digest, but does not by itself verify complete
projection reconstruction, stored vector association, recovery or coverage. Its
summary explicitly keeps these acceptance checks pending.

Run cancellation, provider/inference failure and interrupted-recovery controls
separately in the approved isolated environment. Reconcile stored documents,
projection/coverage, vectors and claims before retrying uncertain operations. A
request timeout/client disconnect does not establish that server work was cancelled.
Do not increase client timeouts to call failed responsiveness a pass. Stop for OOM,
unexpected restart, stalled progress, repeated request failure or identity/resource
drift, and retain the partial record.

## Correlate resources and produce the report

Export the operation interval's Container Apps console logs and collect discovered
Azure app/PostgreSQL metrics using the approved manifest:

```sh
python -m scripts.azure_ingest_trial collect --manifest approved-trial.json \
  --start-time START_UTC --end-time END_UTC --output azure-metrics.json
```

The collector discovers metric names rather than assuming an available vocabulary.
It records available metrics and one-minute observations of CPU/memory/replicas,
restarts, database connections/IO and burst credits where exposed. This is not
sub-second resource sampling. Capture process/cgroup observations inside the service
replica over the same interval as an additional operational trial prerequisite;
microbenchmark process readings do not measure the independently running service.
Cloud working-set metrics are not interchangeable with process RSS. PostgreSQL B2s
CPU credits, unrelated activity and sampling gaps belong in the comparison.

The report must retain image/build/model identities, workload hashes, actual tokens,
passage/coverage counts, repeated-run statistics, cold versus warm results,
instrumentation overhead, resource/cost differences, provider limits and all failed
or partial cases. Identify the measured bottleneck and recommend batching,
allocation, contention management or another evidenced change. A negative result
is useful evidence. Export evidence before deleting only the recorded trial-owned
resources; verify deletion and list remaining billable resources. Code/CI/review
readiness, live Azure measurement, merge, deployment and ticket acceptance remain
separate outcomes.
