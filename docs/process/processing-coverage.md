# Processing coverage and publication

The coverage components in `docs/fs/sage/sage_core_api.openapi.yaml` define SAGE-owned
Tier 1 evidence under CAS-ADR-059. The six dimensions are independent. A terminal
pipeline status describes execution and does not establish any coverage dimension.
Historical records return unknown scope; provisioning never infers completeness or
schedules corpus processing.

A pending dimension has admitted work without established evidence. Unknown means
no trustworthy evidence is available. Not applicable means the dimension does not
apply. Limited scope describes established observations without claiming an exhaustive
source. Complete requires the applied processing identity, exact known counts where
counts are supplied, and no unknown remainder. Units remain independent; the contract
has no overall percentage. Source retention records durable storage, while the existing
source and stored hashes identify provenance and the expected retained bytes. Retention
does not assert a fresh integrity audit.

`begin_processing_attempt` increments a durable document fence without changing served
artifacts or their dimension generations. `publish_processing` checks that fence under
a database row lock, replaces an affected artifact group and its evidence in one
transaction, and leaves other dimensions intact. A stale attempt changes nothing.
Passage replacement publishes projection, keyword and embedding evidence together;
abstraction publication commits its document surface, generated text and evidence
reference together. Different groups may settle independently. A replacement that
fails before publication leaves the earlier served generation available. Existing
producers without trustworthy scope facts publish explicitly unknown evidence.

Pipeline observations use the same attempt fence and the existing `pipeline_status`
authority. They do not create a schedule or a second queue. Recovery derives work from
that status, retains established coverage, and allocates replacement identity rather
than converting a stale in-progress marker into completed evidence.

Passage/coverage reads use one repeatable database snapshot. Scored retrieval carries
the snapshot's coverage with its excerpts, including across hybrid fusion. Full,
light and automatically reduced document summaries preserve coverage. Source and
projection download recipes carry the originating coverage. Raw byte streams keep
their existing byte-only contract; callers obtain evidence through document reads.

The summary is bounded to 65536 UTF-8 bytes. Each dimension permits 8 distinct count
units, 16 limitation strings of at most 512 characters, and 64 location ranges. A
manifest permits 32 location ranges, occupies at most 16384 canonical JSON bytes,
and carries a source hash, generation and content hash matching its summary. Its
SHA-256 covers compact sorted-key JSON containing `entries` and `unknown_remainder`.
Effective configuration has at most 32 keys and 4096 serialized bytes. These limits
bound the record itself; aggregate counts and unknown remainder avoid an exhaustive
omission ledger.

Normal startup provisioning adds a nullable coverage column to every attached vault
idempotently; null is legacy unknown evidence. No manual operator migration is added. No data backfill or live reprocessing is required. Reopening a
restored legacy store follows the same provisioning path. Purge removes coverage
with its document; manifests are embedded bounded detail rather than separate orphans.
Maintenance passage replacement invalidates affected evidence transactionally unless
it publishes fresh evidence through the shared seam. Metadata changes preserve source and selected passage scope. Rewriting derived
keyword text invalidates established keyword evidence; rewriting a document surface
invalidates its established abstraction evidence. Unsupported producer facts stay unknown.
