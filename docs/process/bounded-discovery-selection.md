# Bounded discovery evidence

CAS-ADR-059 separates readable source content from deterministic discovery evidence.
`SourceAdapter.discover_facts` exposes a lazy native-fact stream. The shared
`select_discovery` service produces immutable authored excerpts, compressed source
associations and separately typed derived observations. `capture_inspection_attempt`
and `publish_inspection` settle inspection through the existing attempt fence.
New ingest, recompute and recovery projection attempts inspect facts and settle
inspection before inference. The shared selector also selects complete authored
passage inputs using the embedding provider's exact tokenizer. Content publication
and keyword scope are independent of that selection. Abstraction inputs and
spreadsheet preview settings retain their separate contracts.

## Effective policy and units

`SelectionConfig` in `sage/services/discovery_selection.py` is the policy authority.
Configuration is explicit per call, validated before iteration and frozen in the
returned identity with delivered-source SHA-256, adapter version, effective adapter settings,
FactLimits, selection settings, policy version and an explicit tokenizer identity. A tokenizer is a deterministic token-count callable;
no inference provider participates. Callers must identify the actual tokenizer,
not an anticipated later embedder. Defaults changing in a later version do not
reinterpret an earlier package.

| Limit | Default | Hard maximum | Unit |
| --- | ---: | ---: | --- |
| Inspected values | 100,000 | 400,000 | scalar facts, including empty and machine-shaped values |
| Regions | 256 | 1,024 | distinct region identifiers |
| Fields | 1,024 | 4,096 | distinct region/field pairs |
| Inspected rendering | 16 Mi | 64 Mi | Unicode characters |
| Retained distinct inputs | 8,192 | 32,768 | complete contextual inputs |
| Selected tokens | 8,192 | 32,768 | tokenizer-reported tokens |
| Selected excerpts | 1,024 | 4,096 | distinct inputs |
| One tokenizer input | 32,768 | 32,768 | Unicode characters, context included |
| Retained input/association payload | 16 MiB | 64 MiB | UTF-8 bytes plus reserved run storage |
| Association runs | 16,384 | 65,536 | contiguous source-order runs |
| In-flight embedding inputs | 8 | 32 | distinct complete inputs per provider call |
| Keyword characters | complete supported projection | 64 Mi when explicitly limited | authored content and relative structure characters |

A scalar fact is one cell, native scalar or bounded narrative segment, not one
table row. Source parser work
has separate admission, depth, node, row and column bounds in `FactLimits`;
selection does not redefine those units. Existing ZIP guards remain independently
enforced before workbook parsing (256 MiB expanded bytes, 10,000 members and
compression-ratio checks). These selector limits qualify inspection or selection;
they do not authorize
parsing beyond the separate projection-admission limits below.

## Classification and authored evidence

YAML set, ordered-map, pairs and binary tags are retained as authored lexical
lines, including when nested, with explicit unresolved-native limitations.
Their Python set/tuple/byte representations are not selected as source text,
and binary content is not implicitly decoded. This uses the same bounded,
incremental lexical-line reader as YAML aliases, without building a parallel
whole-source line list. The readable projection keeps its existing parser and
representation; lexical fallback establishes useful authored evidence while
leaving full native inspection unresolved.

Native nulls, booleans, numbers and dates produce inventory observations. Exact
lowercase `true`/`false`, canonical decimal/exponent syntax, valid ISO calendar
dates (`YYYY-MM-DD`) and UUID lexical shapes receive their corresponding syntax
classes. Everything uncertain remains text. Field names never determine class or
significance. Empty text contributes inventory but no excerpt. Text inputs include
source-authored context where supplied by the adapter. Derived structural locations
are metadata, not purported quotations. Unsupported embedded encodings remain
text; suggestive headers do not license application-specific decoding.

Narrative paragraphs are streamed as contiguous segments with stable Unicode
character offsets. Their complete contextual input has a UTF-8 byte ceiling,
`narrative_input_bytes`: the adapter/factory default comes from
`SelectionConfig().max_tokens` (8,192), and ingestion passes its frozen effective
selection budget under the explicit `utf8-byte-tokens:1` sizing rule. The read-only
`FactStream.narrative_input_bytes` records the actual setting in processing identity
facts; two different segmentation settings cannot silently share an identity.
Each segment reserves the authored context's UTF-8 bytes and the two newline
separator bytes before adding content. Iteration sizes individual Unicode units,
without encoding the entire source or splitting a unit's UTF-8 representation.
If context or one Unicode unit cannot fit, the whole unit and authored context
remain available for truthful `selection_budget` exclusion; context is never
silently dropped, and iteration always advances. This deterministic byte sizing
is not a future embedding model's token estimate and does not alter readable
projection or passage inputs.

An input exceeding the per-input character guard is omitted with a limitation,
never silently truncated. Complete contextual input equality alone deduplicates
inference inputs; similar strings and equal values with different authored context
remain distinct. Occurrences are compressed only when their source-order positions
are adjacent. Endpoint locations and exact run counts remain established even
when another occurrence cannot be retained. Storage reserves 4,160 bytes per run
for bounded endpoint text and numeric bookkeeping, in addition to input bytes.
Reaching a run or payload bound makes association completeness false.

## Deterministic allocation

Within bounded inspection, inputs that all fit are retained in source order.
Otherwise the distinct-input dictionary keeps the lowest SHA-256 priorities of
complete input text under candidate and byte budgets. This bounded deterministic
reservoir can admit late text without an unbounded distinct-value dictionary.
Eviction marks association accounting incomplete: prior occurrences of a later
readmitted input cannot be reconstructed without rereading the source.

Selection groups retained inputs by region and field, ordered lexically, with
source order and location as stable ties. Excerpt slots cycle fairly across groups.
Within each group, evenly spaced integer positions include both endpoints when
at least two slots are available; a single slot selects the middle position.
Each group first receives an equal token share. A second pass redistributes unused
tokens in stable group/position order. Selected excerpts return in source order.
Token budgets can leave a group unrepresented when even its individual input does
not fit. These rules claim measurable spread, not semantic importance or complete
recall. Inventory and retained-distinct counts explicitly describe inspected and
retained scope; they are not full-source cardinality estimates.

## Partial scope and publication

Inspection stops at its ceiling without advancing an iterator merely to count the
remainder. At an exact ceiling, an untested remainder remains unknown. Structural
or rendering bounds may require reading one boundary fact, which is not included
in observed counts. Cancellation returns an interrupted package with no completed
selection. Parse errors propagate as failures rather than completed evidence.

The evidence package records observed fact occurrences (including narrative
segments) and characters, the last established location, limitations and separate
inspection/association completeness.
A selected subset does not make inspection incomplete; it qualifies selection.
The inspection publisher records these fact observations as `occurrences`, preserving
the coverage contract's existing units. It publishes a bounded summary, not a
complete inventory manifest, and never settles embedding or abstraction coverage.
Identity is captured before work, validated against the package, and publication
uses the caller's generation fence. A stale producer cannot publish after another
attempt or an unknown-scope maintenance publication invalidates its generation.

Per-input field association accounting reserves another 4,224 bytes for bounded
region/field text and counters. Cross-field duplicates keep one complete input
and associations for every established field; each field's retained-distinct and
known-repetition observations count only its own occurrences. Allocation uses the
first source occurrence as a shared input's representative group; selecting that
input also represents its established occurrences in other groups. Machine-shaped
native values can supply separately identified `structural_label` excerpts from
actual authored key/sheet labels. Derived path syntax alone is never a label quote.
The current pipeline tokenization rule is `utf8-byte-tokens:1`: each UTF-8 byte is
one conservative sizing token. It performs no model load or inference, and is
identified independently of any later embedding provider's tokenizer.

## Live configuration and projection admission

Existing request `config` and per-format vault `adapter_defaults` accept
`discovery_selection` and `discovery_facts` mappings. The existing recursive merge
applies: request keys override vault keys; unspecified nested keys retain vault
values, then the shared dataclasses supply defaults. Unknown nested options,
non-mapping values and values outside hard maxima are refused as adapter
configuration errors before retention. For example:

```json
{
  "discovery_selection": {"max_values": 20000, "max_tokens": 4096},
  "discovery_facts": {"max_depth": 48, "max_parse_nodes": 200000}
}
```

`discovery_facts` supplies default source admission of 64 MiB (hard 256 MiB),
native nesting depth 64 (hard 128), parser nodes 400,000 (hard 1,600,000),
source rows 100,000 (hard 400,000), and columns 256 (hard 1,024). Source size is
checked before reads; lexical/event/SAX/ZIP guards run before projection's whole
native materializers. YAML expansion accounting preserves bounded benign aliases
and refuses recursive or excessive expansion. Byte, depth, parser-node and row ceilings govern structured projection as well
as fact inspection. Delimited-text column ceilings bound projection parsing.
Spreadsheet previews retain their existing fixed width clamp; a wider workbook
records limited fact inspection instead of a new width-only refusal. Refusal retains no
claim of a complete readable projection or keyword index.

These source/parser ceilings are an immediate caller adaptation under the
security-exposure exemption. Safe local synthetic measurements through the prior
adapter show a 203-byte YAML merge source producing 341 actual flattened mapping
pairs, while 387 bytes produce 87,381 pairs. A 18,657-byte nested JSON source
produces 630,114 projected characters. The mechanisms amplify memory/work under
attacker-controlled syntax even when retained source bytes are small; merely
limiting the later selector does not protect the earlier parser. These are
materializer measurements, not inference costs or model token counts.

Full discovery configuration is frozen before work and bounded to 64 KiB of
canonical JSON, 4,096 JSON nodes, depth 32 and 65,536 source string characters.
Raw request/vault mappings are checked before recursive copying or merging.
When the full identity exceeds coverage's existing 4 KiB configuration summary,
coverage retains complete selection/fact settings and an SHA-256 of the canonical
adapter settings, with `adapter_configuration_omitted: true`. The immutable evidence
package retains the full effective configuration; the compact coverage summary
identifies it without claiming to enumerate omitted settings. This is the
inspection identity; other stage identities keep their own established scope.
Native structured/spreadsheet adapter entry points enforce the same safety guards
for direct script callers. The guarded worker also checks before dispatch; this
bounded duplicate check introduces no additional scheduler or unbounded queue.

The selected adapter owns format semantics. Native facts and parser guards apply
to the structured-data and workbook adapters; an explicitly selected narrative
adapter retains narrative semantics even when the filename has a structured-file
extension. A suffix alone does not override the caller's selected source type.

Established inspection publishes an optional typed `selection_summary` through
processing coverage. Its `outcome` is `complete`, `limited` or `interrupted`,
independent of inspection scope: complete inspection may still have limited
selection. `selected_inputs` counts distinct complete selected inputs and
`selected_tokens` counts their sizing tokens under the parent identity's
`tokenization` rule. `established_occurrences` sums retained selected-input
associations and is a lower bound unless `associations_complete` is true.
`boundary`, when present, is the last inspected source location. Interrupted
selection retains zero selected evidence. Legacy or unestablished inspection
omits this summary. It shares the inspection generation, attempt fence and
identity; it establishes no embedding or abstraction coverage.

## Passage execution and reprocessing

Source-fact inspection and passage selection have separate units and identities.
Passage selection applies the same deterministic allocation and complete-input
deduplication algorithm to each stored passage's authored address plus exact text.
Its compressed occurrence runs identify stable passage indexes. Model token counts
are exact; source-fact byte sizing remains separately identified. An oversized model
input is excluded, never silently clipped. Every selected duplicate retains all
established passage associations within the existing association bounds.

All projected passages publish before inference, with genuine NULL vectors where
none exist. Keyword indexing covers the full supported projection by default.
`discovery_selection.max_keyword_characters` is an optional finite limit; null keeps
the complete supported projection. A limited passage is still readable but cannot
satisfy a keyword term. PostgreSQL's own tsvector ceiling is handled separately:
readable bytes survive and keyword coverage records `backend_keyword_limit`.
Existing stores must run the idempotent vault migration to rebuild the generated
keyword expression before using the new omission path. Migration never treats old
zero vectors or a terminal pipeline status as proof of embedding coverage.

`max_in_flight` limits distinct complete inputs per call (default 8, maximum 32).
Selection's existing `max_excerpts` and `max_tokens` limit total admitted distinct
inputs and exact tokens. The existing provider remains the shared backpressure
authority. A provider call need not be a model inference batch. Association vector
writes use windows of at most 128 references and publish evidence atomically under
the durable attempt fence. Earlier committed increments survive later failures.

The document's internal source ledger debits before inference. Retry and recompute
retain consumption, including uncertain interrupted calls, and reuse only positively
established matching input vectors. Exhaustion or an unapproved scope change records
a limitation rather than opening a transient retry loop. A source hash replacement
records a new source decision. Changing scope for the same source requires an
explicit operator decision, supported by the stored-passage reindex script's
`--reprocessing-decision` argument; the ledger retains its reason, prior consumption
and attempt. Omitting it is a conservative retry, and a second completed run consumes
no further inference. Normal ingest/recompute never silently invokes that reset.
An unchanged decision identifier is idempotent and retains consumed or uncertain
debits. An unapproved maintenance scope change is refused before changing the
attempt, checkpoints, coverage or artifacts, with a transactional recheck before
replacement. The served `migrate_vault` operator action authorizes its named passage
backfills; each records a decision bound to the source and adapter transition or
input bound. Helpers and startup do not infer that authorization. Retrying the same
transition preserves consumption. Migration tests exercise this actual entry point.
The explicit reprojection script captures the same inspected effective policy as
ingest and binds its decision to that configuration and inspected source digest.
Durable identity comparisons use canonical JSON, so a database key-order change
cannot turn metadata reconciliation into an unapproved policy change.

`available_passage_count`, `keyword_indexed_passage_count`, `embedded_passage_count`
and `matched_passage_count` describe distinct populations. Unknown optional counts
are absent/null. The existing `matched_chunk_count` continues to count sections.
Document-level hits contribute zero passages. Projection and keyword coverage remain
available on reads and search, including partial embedding failures; empty results
over unknown or limited discovery scope do not establish source absence.

Document vectors used for independent semantic admission contain authored title/tag
text only. Derived descriptions retain their keyword ranking and orientation role
for authored-qualified results. Legacy document vectors are retained with unknown
authored provenance and excluded from independent semantic admission until explicit
reprocessing establishes it. Reabstracting updates derived orientation without
spending another document-vector inference allowance. Metadata changes to authored
title/tag inputs retain the old vector but quarantine its independent admission;
orientation-only changes preserve established authored provenance.
Unchanged recomputation reuses the established document vector without another
provider call. Replacement retains old vectors for orientation but requires matching
authored input and source generation to preserve their admission provenance.

Inspection binds two byte identities. The processing identity's `source_hash` keeps
established delivered-source provenance and the durable publication fence. Frozen
selection configuration separately records `inspected_source_hash`, the SHA-256 of
the retained bytes actually projected and inspected. These may differ when a source
store rewrites an Office package. Ingest (including force and resident reuse),
recompute and recovery preserve both; a previously unrecorded resident source uses
its retained digest for both identities. The bounded published summary retains the
inspected digest; its full-configuration digest also binds it when settings are
omitted. This changes no source bytes, stored provenance, or publication comparison.

## Bounded abstraction

Generation consumes the immutable authored `EvidencePackage` captured by shared
inspection. Its complete selected inputs already contain source context. Prior
abstracts and other document-surface prose are excluded by construction. Computed
observations remain separately typed. Request construction may supply them as
explicitly identified data after the authored excerpts. Each measurement states
its operation, unit, region/field, inspected boundary, source hash and processing
generation. Only inspected-value counts are supplied, exact within that scope.
Retained-distinct and repetition counters describe rendered candidate inputs,
which can be structural labels or include context; they are withheld from model
evidence because they do not establish native-value distinctness or repetition.
No full-source totals are inferred. Labels receive the same syntax neutralization
as source text.
A protected system instruction permits descriptions of supplied values while
prohibiting new calculations, extrapolation, importance or comprehensive claims.
Caller metadata and source assertions are never parsed into this evidence class.

Authored inputs fit first; complete observation groups fit in the remaining
protected-prompt allowance, at deterministic spread positions. Omitted groups
add `abstraction_observation_budget` to limited evidence coverage. Observations
do not increase the sizing target or authorize another generation call.
Admission fences source/selection identity and processing attempt before inference;
the generation marker describes the current admitted use of the retained evidence,
not a claim that reabstract inspected the source again.

The acronym check and calibrated repair retain exactly their prior authored/context
baseline. Added observations cannot attest expansions, including expansions inside
their labels. The cardinal and structure instruments retain their recording posture;
no general numeric, identifier or paraphrase validator is claimed. The behavioral
evaluation command below retains raw outputs separately from gloss-repaired text:

```sh
.venv/bin/python -m scripts.evaluate_abstraction_faithfulness \\
  --provider qwen --repeats 3 --output /absolute/evaluation.json
# For the configured hosted binding, supply ANTHROPIC_API_KEY through the environment:
.venv/bin/python -m scripts.evaluate_abstraction_faithfulness \\
  --provider anthropic --repeats 3 --output /absolute/evaluation.json
```

Both arms use identical synthetic evidence. These direct-provider trials are not
end-to-end stored/served verification, and must be adjudicated per case. Repeated
calls measure behavior; they are not a repair retry policy. Reconstructed acronym
cases are distinguished from retained observed outputs. Changes to destructive
repair authority need independent calibration and
the applicable architectural decision.

The target output allowance in tokens is
`min(1500, configured_output_cap, floor(base + selected_words * text_scale + min(region_field_pairs, 32) * breadth_scale))`,
with defaults `base=150`, `text_scale=0.02`, `breadth_scale=12`, and
`configured_output_cap=1500`. The effective target is at least one token so the
provider receives a valid call; adequate prose is never padded to reach it.
Words and region/field pairs describe supplied authored evidence under its
selection identity. Vectors, information-density scores, variance, extra
embeddings and generative selection are unnecessary.

Legacy `max_abstract_tokens`, `base_abstract_tokens` and `tokens_per_word` settings
remain accepted. Values beyond shared effective bounds warn and clamp; output
never exceeds 1500, the base contribution 1500, or text scale 1. The names now
measure selected authored evidence, with no full-source density promise.
`tokens_per_breadth` accepts 0 through 100. No existing abstract is silently
regenerated when these defaults change.

`max_input_tokens` bounds each complete protected prompt (default 16384, maximum
32768). Input accounting uses conservative UTF-8 byte-token upper bounds for
system instructions, framed source and 1024 tokens of provider-envelope headroom.
The local provider also checks the actual serialized prompt against that
reservation and its context window. Request construction supplies native region/field context once per group. If the
complete protected prompt still does not fit, it reduces complete excerpts at stable
source-spread positions, recomputes sizing over the supplied subset, and records
`abstraction_prompt_budget` plus its smaller represented-token count before generation.
Providers may refuse an additional context overrun; they cannot silently shrink
established evidence under an unchanged coverage claim.

`max_generation_calls` defaults to three and cannot exceed three. Each queued job
is also bounded by `max_attempts`; a smaller per-job retry count does not reset the
source-wide call allowance. Cumulative defaults are 49152 input tokens (maximum 98304) and
4500 reserved output tokens (maximum 4500). A durable source/selection ledger
reserves a call and its complete prompt/output allowances **before** inference.
Uncertain, failed and interrupted calls retain their reservations across retry,
reabstract, restart and recompute; changing the selection identity or allowances
refuses further consumption instead of resetting the ledger. A new delivered-source
`identity.source_hash` establishes a new attributable source scope. Changing only
the inspected retained-byte digest changes selection identity and is refused as
`scope_changed`; it does not reset prior consumption. Ordinary reabstract is
not permission to expand consumed work. Hosted SDK generation retries are disabled.
Bounded local work validates the actual first generation instead of issuing an
additional startup probe; direct legacy adapter calls retain their probe behavior.

The bounded package is persisted separately from passages in
`documents.abstraction_state`. Reabstract and startup recovery read that package
without loading every passage or vector. Missing legacy evidence is rebuilt only
by bounded retained-source inspection with an independently verified hash; native
formats use the shared fact reader, Markdown/text use an explicitly limited
lexical prefix. Unsupported legacy formats require explicit source reprocessing;
unverified stored passages are never promoted to trusted evidence.

`processing_coverage.abstraction_evidence` publishes when the input is established,
before generation. Pipeline status independently records success, failure,
interruption or skipping. Disabled generation before construction leaves evidence
unknown; failure after construction preserves established scope. A failed
replacement preserves the previously served abstract. The nullable
`served_abstraction_generation` identifies that abstract's generation separately
from newer attempt evidence; null means historical identity unknown. Publication
of a replacement abstract and its evidence is atomic under the processing fence.
A text-only maintenance repair preserves the served generation, current attempted
evidence, cumulative allowances and recovery checkpoint. It does not allocate a
processing attempt or relabel evidence. Repair publication compares the expected
prior abstract and processing attempt atomically; stale repairs are refused.

When no generated abstract exists, document-level orientation can show a labeled
**Derived inventory** of established inspection counts and their scope. It remains
outside authored passages and matchable document text, and cannot independently
admit a result. Retained source, readable projection and completed keyword/vector
work remain usable after abstraction failure. Operators must apply the ordinary
idempotent vault schema migration for the nullable durable-state column.

## Hybrid description publication

`abstraction.hybrid_description` defaults to false. Enabling it requires the
companion architecture and contract alignment; existing vaults retain their
configured abstraction behavior. In hybrid mode the provider receives only
selected authored evidence. SAGE appends at most eight whole inspected-value
measurements with region, field, inspected boundary and completeness labels.
Candidate distinctness and repetition do not establish native value counts.
Both components share the evidence source and attempt identity.

The final description reserves its entire configured bound before generation.
UTF-8 bytes conservatively bound tokens; the final bound is at most 1,500 bytes.
Complete observations fit after room is reserved for prose. Empty or oversized
prose falls back to complete contextual source units in stable source order,
quoted with source labels. At most three units fit within 256 bytes (therefore
also within 256 tokens and the separate 2,048-byte ceiling), subject to the
stricter remaining final bound. Units are skipped whole, never clipped. If none
fits, the description is unavailable and old prose is cleared. Retained source
and readable passages remain available.

`processing_coverage.abstraction_quality` is a distinct quality subrecord in the
existing atomic processing envelope; it does not alter any coverage dimension or
replace pipeline outcome. Its mode distinguishes historical unknown quality,
recording generation, class-screened generation, extractive fallback and
unavailable description. Checks, findings, exact accepted calibration references,
source/attempt identity, scoped measurements and raw-candidate digest accompany
it. Raw provider output is bounded to 65,536 characters in private attempt
state, excluded from public serialization and retrieval surfaces. Publication
atomically replaces description, orientation surface, quality and diagnostics
under the existing source/attempt fence. Maintenance prose replacement invalidates
prior quality to unknown.

Three narrow signatures record unsupported machine identifiers, bounded
count/structure assertions and a limited identifier-range role shape. They are
proxies, with known false positives and semantic gaps. Recording findings do
not withhold output. Existing acronym and opener repairs keep their established
scope. `abstraction.withholding_classes` defaults empty and refuses unaccepted
classes. The accepted registry is empty; adding a receipt requires a reviewed
change binding the accepted frozen report and exact implementation digest.
Changed implementation invalidates the receipt. No extra inference or retry is
introduced by withholding.

`python -m scripts.evaluate_abstraction_publication --help` describes the separate
calibration report tool. It checks frozen implementation identity, independent
blinded labels, source/template split leakage, deduplicated evidence populations,
per-provider positive and faithful minima, fallback safety/usefulness and exact
binomial confidence intervals. Unknown labels and shortfalls block eligibility.
Eligibility never enrolls a class: owner acceptance and reviewed receipt enrollment
remain separate. Synthetic direct-provider trials and disposable stored/served
checks provide regression evidence, not independently adjudicated held-out
calibration or hosted deployment acceptance.
