# Inactive abstraction regression diagnostic campaign

The portable `abstraction-regression-campaign.json` freezes ten synthetic case
families, exact UTF-8 source and selected evidence bytes (base64 plus SHA-256),
source/selection identities, provider settings, expected behavior, arms, stage
allocation and repeat policy. JSON/JSONL controls use native source discovery;
ordinary prose is a complete authored text unit. Two original synthetic smoke
sources and the retained supporting synthetic artifacts are included verbatim.
Historical served descriptions do not establish raw provider responses or usage.
Unknown historical raw outputs and billing remain explicitly unknown.

This preparation does not authorize provider calls. The script imports no live
provider, reads no vault and exposes no dispatch CLI. Its injected callable is
for deterministic tests and must perform at most one synthetic call without
internal retries. A future live adapter needs separate authorization and must
freeze its exact final requests and actual observed model/settings before use.
Provider settings are proposed values, not observed dispatch evidence.
The hosted model alias has no immutable provider revision; `revision: null` is
an explicit limitation. The implementation hash is a snapshot, and report output
flags any subsequent implementation drift.

Run from the repository using its locked environment:

```sh
.venv/bin/python -m scripts.diagnose_abstraction_regressions prepare \
  --spec docs/development/abstraction-regression-campaign.json \
  --manifest /absolute/evidence/campaign-manifest.json \
  --ledger /absolute/local-state/abstraction-calls.sqlite
.venv/bin/python -m scripts.diagnose_abstraction_regressions status \
  --manifest /absolute/evidence/campaign-manifest.json
.venv/bin/python -m scripts.diagnose_abstraction_regressions report \
  --manifest /absolute/evidence/campaign-manifest.json
```

Prepare creates the manifest, ledger and a single campaign binding at
`~/.local/state/cas/diagnostics/abstraction-factual-regressions-v1.binding.json`
exclusively. The manifest freezes that directory; retain it as the campaign
receipt across resumes. Deterministic tests replace this root with a temporary
fixture directory; this preparation task creates no global campaign binding. A second
preparation cannot choose another ledger and restart the budget. Keep this
binding and the ledger together across resumes. Missing, mismatched or
inconsistent state fails closed. An interrupted preparation can leave an
incomplete binding; do not delete it to retry a consumed campaign. Recovery
requires reconciling retained state explicitly. The binding is local campaign
state: concurrent processes must share its frozen home; multi-host operation is unsupported and
requires a separately designed central ledger. These integrity checks detect
accidental changes, not a privileged operator rewriting all state and hashes.

SQLite ledgers are rejected inside Git checkouts and recognized cloud sync
paths. Choose a local durable directory; do not use temporary storage for an
actual campaign. Every attempt reserves a slot in a `BEGIN IMMEDIATE`
transaction with full synchronous durability before dispatch. Failures, retries,
unresolved crash reservations, providers, arms and stages share the same hard
120-call total. Baseline, repaired and validation allocations each permit 40.
A refused call never invokes the dispatcher. A crash reservation remains spent;
a retry requires another reservation and unique attempt identity.

Results separate raw, licensed-repaired, composed and actual stored/served
bytes. An unavailable stage is `null`, never copied from another stage. Usage is
also separately recorded and may be `null`. The existing publication calibration
instrument receives only licensed-repaired outputs. Its missing labels,
insufficient independent populations, repetitions and split leakage remain
blockers. All initial cases are development cases. Empty reports explicitly
flag their unexecuted placeholder; that row is not an observed result. There
are no new public publication classes or runtime promotions. Expected behavior
text is a diagnostic specification, not an independently adjudicated label or
factual acceptance verdict.
