# Bounded discovery evidence

CAS-ADR-059 separates readable source content from deterministic discovery evidence.
`SourceAdapter.discover_facts` exposes a lazy native-fact stream. The shared
`select_discovery` service produces immutable authored excerpts, compressed source
associations and separately typed derived observations. `capture_inspection_attempt`
and `publish_inspection` settle inspection through the existing attempt fence.
New ingest, recompute and recovery projection attempts inspect facts and settle
inspection before inference. Selection does not execute embeddings or abstracts,
replace projection, sample keyword indexing, or automatically reprocess stored
documents. Existing embedding/abstraction inputs and spreadsheet preview settings
remain unchanged.

## Effective policy and units

`SelectionConfig` in `sage/services/discovery_selection.py` is the policy authority.
Configuration is explicit per call, validated before iteration and frozen in the
returned identity with source SHA-256, adapter version, effective adapter settings,
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

A scalar fact is one cell or native scalar, not one table row. Source parser work
has separate admission, depth, node, row and column bounds in `FactLimits`;
selection does not redefine those units. Existing ZIP guards remain independently
enforced before workbook parsing (256 MiB expanded bytes, 10,000 members and
compression-ratio checks). These limits apply to inspection; exhausting one records limited inspection and
does not narrow the existing source-admission API.

## Classification and authored evidence

Native nulls, booleans, numbers and dates produce inventory observations. Exact
lowercase `true`/`false`, canonical decimal/exponent syntax, valid ISO calendar
dates (`YYYY-MM-DD`) and UUID lexical shapes receive their corresponding syntax
classes. Everything uncertain remains text. Field names never determine class or
significance. Empty text contributes inventory but no excerpt. Text inputs include
source-authored context where supplied by the adapter. Derived structural locations
are metadata, not purported quotations. Unsupported embedded encodings remain
text; suggestive headers do not license application-specific decoding.

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

The evidence package records observed scalar facts and characters, the last
established location, limitations and separate inspection/association completeness.
A selected subset does not make inspection incomplete; it qualifies selection.
The inspection publisher records scalar observations as `occurrences`, preserving
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
