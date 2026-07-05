# SAGE Adapter Tests

Tier 2 behavioral tests for production adapter implementations across the
adapter surface: nomic-embed-text EmbeddingProvider, Qwen3 AbstractionProvider
(eager and lazy loading paths), and the source adapters (Markdown, Docx,
Xlsx, Word template `.dotx`, PDF). Content-store adapter tests (formerly
Sections 2 and 10, embedded LanceDB) are retired along with the embedded
storage backend (CAS-ADR-042); the Postgres content-store adapter's
behavioral coverage lives in `tests/sage/test_content_store_postgres.py`.

These tests validate that the concrete adapter implementations satisfy the
abstract interfaces defined in `sage/adapters/interfaces.py` (for retrieval
and abstraction adapters) and `sage/source_adapters/base.py` (for source
adapters) with real storage and real artifacts. Distinct from the SAGE
behavioral tests (TEST-SAGE-BH-*), which validate service-layer logic
against these same interfaces using stubs.

Each test encodes a design decision made during adapter specification.
Tests are grouped by adapter in implementation dependency order: embedding
provider first, then abstraction, then source adapters in the order they
were added to the codebase.

Test environment: source adapter tests construct artifacts in `tmp_path` and
exercise the adapter directly without going through the ingestion pipeline.

---

## 1. nomic-embed-text EmbeddingProvider

### TEST-SAGE-AD-001: Embedding dimension is 768

**Artifact:** `sage/adapters/interfaces.py` (EmbeddingProvider)
**Category:** shape
**Decision:** nomic-embed-text produces 768-dimensional vectors. The provider validates this at init by embedding a probe text.

**Precondition:** nomic-embed-text model available via sentence-transformers.

**Input:** `embed(["The report claims a novel method for data synchronization."])`

**Expected:**
- Returns a list containing one vector
- The vector has exactly 768 elements
- All elements are floats

**Rationale:** The LanceDB content store schema defines a fixed-size vector column
of 768 dimensions. A dimension mismatch between embedding provider and content
store would cause silent index corruption or runtime errors. Fail-fast validation
at init prevents this.

### TEST-SAGE-AD-002: Batch embedding preserves input order

**Artifact:** `sage/adapters/interfaces.py` (EmbeddingProvider.embed)
**Category:** batch
**Decision:** N input texts produce N output vectors in the same order.

**Precondition:** Provider initialized.

**Input:** `embed(["alpha", "beta", "gamma"])`

**Expected:**
- Returns exactly 3 vectors
- Each vector has 768 dimensions
- The vector for "alpha" is at index 0, "beta" at index 1, "gamma" at index 2

**Rationale:** The ingestion pipeline zips chunks with their embeddings by index
position (`sage/services/ingestion.py`). Order mismatch would assign wrong
embeddings to chunks, degrading retrieval quality silently.

### TEST-SAGE-AD-003: Same text produces identical embeddings

**Artifact:** `sage/adapters/interfaces.py` (EmbeddingProvider.embed)
**Category:** determinism
**Decision:** Embedding is deterministic for the same input text.

**Precondition:** Provider initialized.

**Input:** Call `embed(["reproducibility test"])` twice.

**Expected:**
- Both calls return vectors that are element-wise identical (within floating-point tolerance of 1e-6)

**Rationale:** Deterministic embeddings ensure that re-indexing a document produces
the same vectors, and that query embeddings are stable across identical queries.
Non-determinism would cause retrieval inconsistency.

### TEST-SAGE-AD-004: Output vectors are L2-normalized

**Artifact:** `sage/adapters/interfaces.py` (EmbeddingProvider.embed)
**Category:** normalization
**Decision:** All output vectors have L2 norm approximately equal to 1.0. Enforced via `normalize_embeddings=True` in sentence-transformers.

**Precondition:** Provider initialized.

**Input:** `embed(["Short text.", "A significantly longer passage with multiple sentences about various topics including report law, data management, and retrieval systems."])`

**Expected:**
- For each output vector, `sqrt(sum(v_i^2))` is within 1e-4 of 1.0

**Rationale:** The LanceDB content store uses cosine distance for vector search.
With L2-normalized vectors, cosine similarity reduces to dot product, which is
both faster and numerically stable. Non-normalized vectors would produce incorrect
similarity rankings.

### TEST-SAGE-AD-005: Similar texts have higher cosine similarity than dissimilar texts

**Artifact:** `sage/adapters/interfaces.py` (EmbeddingProvider.embed)
**Category:** similarity
**Decision:** The embedding model captures semantic similarity.

**Precondition:** Provider initialized.

**Input:** Embed three texts:
- A: "The document describes a method for synchronizing health records."
- B: "A technique for keeping medical data in sync across systems."
- C: "The basketball team scored 47 points in the first half."

**Expected:**
- `cosine_similarity(A, B) > cosine_similarity(A, C)`
- `cosine_similarity(A, B) > cosine_similarity(B, C)`

**Rationale:** Semantic similarity is the foundation of the retrieval subsystem.
If the embedding model fails to distinguish semantically related from unrelated
texts, vector search produces meaningless results. This is a smoke test, not a
comprehensive quality evaluation (that is the role of eval_retrieval).

### TEST-SAGE-AD-006: Empty input returns empty output

**Artifact:** `sage/adapters/interfaces.py` (EmbeddingProvider.embed)
**Category:** edge_case
**Decision:** `embed([])` returns `[]` immediately with no model invocation.

**Precondition:** Provider initialized.

**Input:** `embed([])`

**Expected:**
- Returns an empty list `[]`
- No model inference occurs (completes in under 1ms)

**Rationale:** The ingestion pipeline could encounter a document with no content
chunks (e.g., a file with only metadata and no body text). The embedding provider
should handle this gracefully rather than passing an empty batch to the model,
which could raise an error or return unexpected shapes.

### TEST-SAGE-AD-007: Single text input works correctly

**Artifact:** `sage/adapters/interfaces.py` (EmbeddingProvider.embed)
**Category:** edge_case
**Decision:** A batch of one text is a valid input.

**Precondition:** Provider initialized.

**Input:** `embed(["single input"])`

**Expected:**
- Returns a list containing exactly one vector
- The vector has 768 dimensions and is L2-normalized

**Rationale:** Query embedding in the retrieval service always passes a single
text (`sage/services/retrieval.py`). This must work correctly as a batch of one.

### TEST-SAGE-AD-008: Provider init fails fast if model unavailable

**Artifact:** `sage/adapters/interfaces.py` (EmbeddingProvider)
**Category:** initialization
**Decision:** The provider loads the model eagerly at `__init__` and raises an error if the model cannot be loaded.

**Precondition:** Attempt to initialize with a non-existent model name.

**Input:** Construct provider with `model_name="nonexistent-model-xyz"`.

**Expected:**
- Raises an exception during `__init__` (not deferred to first `embed` call)
- Error message includes the model name

**Rationale:** Deferred loading would allow the vault to initialize successfully
but fail on the first ingestion or query, which is harder to diagnose. Eager
loading surfaces configuration errors at startup.

---

## 2. LanceDB ContentStore (RETIRED)

Covered the embedded LanceDB content-store binding (AD-009..AD-025,
AD-098..AD-106), retired along with the embedded storage backend
(CAS-ADR-042). The Postgres content-store adapter's behavioral coverage
lives in `tests/sage/test_content_store_postgres.py`; the has_chunks
existence-probe contract (formerly AD-098/AD-099, see Section 10 below)
is covered there by `test_has_chunks`.

---


## 3. Qwen3 AbstractionProvider

Tests for the production abstraction provider: Qwen3-30B-A3B-Instruct-2507 via
MLX on Apple Silicon. The provider implements the single-method
`AbstractionProvider` interface (`generate_abstract(text, max_tokens) -> str`).

These tests validate local LLM inference behavior. They are slower than the
embedding and content store tests (model load + generation latency) and should
be marked accordingly in the test runner.

Test environment: requires MLX and the Qwen3-30B-A3B-Instruct-2507 model
weights available locally. Tests that validate model output quality use
conservative assertions (non-empty, bounded length, semantic relevance smoke
test) rather than exact string matching, since LLM output is inherently
variable across model versions.

### TEST-SAGE-AD-026: Provider init succeeds without loading model (lazy loading)

**Artifact:** `sage/adapters/interfaces.py` (AbstractionProvider)
**Category:** initialization
**Decision:** The provider defers MLX model loading to first use. Construction
stores configuration only; no weights are loaded. The model loads on the first
`generate_abstract()` call, with a probe validation at that point. This caps
baseline memory when abstraction has not yet been needed.

**Precondition:** Qwen3-30B-A3B-Instruct-2507 model weights available locally.

**Input:** Construct a Qwen3AbstractionProvider with valid model ID.

**Expected:**
- Construction completes without error
- No MLX model is loaded into memory at construction time
- First `generate_abstract()` call triggers model load and succeeds

**Negative input:** Construct with `model_id="nonexistent-model-xyz"`.

**Negative expected:**
- Construction completes without error (no load yet)
- First `generate_abstract()` call raises RuntimeError
- Error message includes the model ID

**Rationale:** Lazy loading reduces baseline memory by ~16-20 GB when abstraction
is enabled but has not yet been invoked. Fail-fast still occurs, just on first
use rather than at startup. For bulk ingest, the first document triggers the
load; subsequent documents reuse the loaded model.

### TEST-SAGE-AD-027: Generated abstract is a non-empty string

**Artifact:** `sage/adapters/interfaces.py` (AbstractionProvider.generate_abstract)
**Category:** output_shape
**Decision:** `generate_abstract` always returns a non-empty string on success.

**Precondition:** Provider initialized with valid model.

**Input:** `generate_abstract("The document describes a method for synchronizing
patient health records across distributed clinical systems using a
conflict-free replicated data type (CRDT). The approach ensures eventual
consistency while preserving the causal ordering of clinical events.", 200)`

**Expected:**
- Returns a `str`
- Length is greater than 0 after stripping whitespace
- Contains no leading/trailing whitespace

**Rationale:** The document record stores `semantic_abstract` as a nullable
string. A null value means "not yet generated" (pipeline in progress or
abstraction skipped). An empty string would be ambiguous -- indistinguishable
from a generation failure that returned empty output. The provider must return
substantive content or raise an exception.

### TEST-SAGE-AD-028: Output respects max_tokens upper bound

**Artifact:** `sage/adapters/interfaces.py` (AbstractionProvider.generate_abstract)
**Category:** output_constraint
**Decision:** The returned abstract does not exceed `max_tokens` in token count.
The provider passes `max_tokens` as the generation limit to the MLX inference
call.

**Precondition:** Provider initialized with valid model.

**Input:** `generate_abstract(long_document_text, 100)` where
`long_document_text` is a multi-paragraph technical passage (500+ words).

**Expected:**
- The returned abstract, when tokenized by the model's tokenizer, contains
  at most 100 tokens
- The abstract is still coherent (not truncated mid-word or mid-sentence)

**Rationale:** The vault schema defines `max_abstract_tokens` with a minimum of
50. The Example Portfolio vault sets this to 500. The provider must honor this bound
to ensure abstracts are consistently sized for retrieval and display. The
model's generation parameters (not post-hoc truncation) should enforce this,
so output remains coherent at the boundary.

### TEST-SAGE-AD-029: Same input produces identical output (deterministic)

**Artifact:** `sage/adapters/interfaces.py` (AbstractionProvider.generate_abstract)
**Category:** determinism
**Decision:** Same text + same max_tokens produces the same abstract.
Achieved by setting temperature=0 (or equivalent greedy decoding) in the MLX
inference configuration.

**Precondition:** Provider initialized with valid model.

**Input:** Call `generate_abstract(sample_text, 200)` twice with the same input.

**Expected:**
- Both calls return identical strings

**Rationale:** Deterministic output ensures that re-ingesting the same document
(force re-ingestion, BH-019) produces the same abstract. Non-determinism would
cause the semantic_abstract field to change on re-ingestion even when the
source content has not changed, creating spurious update noise.

### TEST-SAGE-AD-030: Short input produces a valid abstract

**Artifact:** `sage/adapters/interfaces.py` (AbstractionProvider.generate_abstract)
**Category:** edge_case
**Decision:** Very short input text (a few words or a single sentence) produces
a valid non-empty abstract without error.

**Precondition:** Provider initialized with valid model.

**Input:** `generate_abstract("Brief note about record linkage.", 200)`

**Expected:**
- Returns a non-empty string
- No exception raised

**Rationale:** Some documents may have minimal content after projection (e.g.,
a short note or a stub document). The provider must handle this gracefully.
The abstract may be shorter than max_tokens -- that is correct behavior for
low-density input.

### TEST-SAGE-AD-031: Long input does not crash

**Artifact:** `sage/adapters/interfaces.py` (AbstractionProvider.generate_abstract)
**Category:** edge_case
**Decision:** Input text exceeding the model's context window is handled without
raising an unrecoverable error. The provider truncates input to fit the context
window before generation.

**Precondition:** Provider initialized with valid model.

**Input:** `generate_abstract(very_long_text, 200)` where `very_long_text` is
50,000+ characters of repeated technical prose.

**Expected:**
- Returns a non-empty string (abstract of the truncated input)
- No exception raised
- The abstract reflects content from the beginning of the input (truncation
  preserves leading content, not trailing)

**Rationale:** EXAMPLE report documents can be lengthy. While the pipeline-level
existing-abstract bypass (planned for step 20) will reduce how often long
documents reach the LLM, the provider must not crash when they do. Truncation
is preferred over failure because the leading content of a well-structured
document (title, abstract, introduction) typically carries the highest
information density.

### TEST-SAGE-AD-032: Abstract is semantically related to input

**Artifact:** `sage/adapters/interfaces.py` (AbstractionProvider.generate_abstract)
**Category:** quality
**Decision:** The generated abstract contains concepts present in the source
text. This is a smoke test, not a comprehensive quality evaluation.

**Precondition:** Provider initialized with valid model. Embedding provider
initialized (for similarity measurement).

**Input:** `generate_abstract(technical_passage_about_health_records, 200)`

**Expected:**
- Embed both the input text and the generated abstract using the embedding
  provider
- `cosine_similarity(input_embedding, abstract_embedding) > 0.5`
- The abstract mentions at least one key concept from the input (verified by
  keyword overlap, not exact substring matching)

**Rationale:** Analogous to AD-005 (semantic similarity smoke test for
embeddings). If the abstraction provider generates text unrelated to the input,
the semantic_abstract field becomes noise rather than a useful retrieval signal.
This test catches catastrophic model failures (wrong prompt template,
garbled output) without asserting specific wording.

### TEST-SAGE-AD-033: LLM runtime error propagates as exception

**Artifact:** `sage/adapters/interfaces.py` (AbstractionProvider.generate_abstract)
**Category:** error_handling
**Decision:** If the MLX inference fails mid-generation (out of memory, model
corruption, hardware error), the exception propagates to the caller. The
provider does not catch and swallow LLM errors.

**Precondition:** Provider initialized with valid model, then model state
corrupted or inference forced to fail (e.g., via monkey-patching the MLX
generate call to raise RuntimeError).

**Input:** `generate_abstract("any text", 200)` with inference rigged to fail.

**Expected:**
- Raises an exception (RuntimeError or subclass)
- Exception message describes the failure
- No partial or empty string is returned

**Rationale:** The ingestion pipeline (BH-024) depends on the exception to set
`pipeline_status: "failed"` and record `pipeline_error`. If the provider
swallows the error and returns empty or partial output, the pipeline would
incorrectly mark the document as `abstraction_complete` with a degraded
abstract, violating the strict quality gate.

---

## 4. Markdown Adapter -- Source Provenance

### TEST-SAGE-AD-034: Markdown adapter extracts source_modified_at from file mtime

**Artifact:** `sage/source_adapters/markdown_adapter.py` (MarkdownAdapter.project)
**Category:** provenance, metadata
**Decision:** The markdown adapter calls `source_path.stat()` to extract the
file's `st_mtime` and includes it in `ProjectionResult.metadata` as an
ISO 8601 string keyed as `"source_modified_at"`.

**Precondition:** Temporary markdown file created with a known modification
time (set via `os.utime` for determinism).

**Input:** `adapter.project(source_path)`

**Expected:**
- `result.metadata["source_modified_at"]` is a string
- Parsing it with `datetime.fromisoformat()` succeeds
- The parsed datetime is timezone-aware (UTC)
- The parsed datetime matches the file's `st_mtime` (within 1-second tolerance)

**Rationale:** File-based adapters are the natural point to extract filesystem
metadata. Centralizing this in the ingestion service would couple it to
filesystem assumptions that don't apply to all adapter types (e.g., future
API-based adapters). The ISO 8601 string format keeps `ProjectionResult.metadata`
serializable without importing datetime into the base adapter module.

---

## 5. Docx Source Adapter

Tier 2 behavioral tests for `sage/source_adapters/docx_adapter.py`
(DocxAdapter). The adapter parses Word document structure via python-docx:
heading extraction from paragraph styles via configurable
`heading_style_map`, table extraction as pipe-delimited text rows, body
paragraphs attached to the nearest preceding heading, content hashing of
raw `.docx` bytes, source-modified provenance via file mtime, and a
`_NumberingEngine` that computes rendered heading number prefixes from
Word numbering definitions (decimal, upperRoman, lowerRoman, upperLetter,
lowerLetter) with counter reset on parent level increment.

These tests exercise the `.docx` branch only. Template (`.dotx`) handling
is specified separately in section 7.

### TEST-SAGE-AD-035: Basic projection returns valid ProjectionResult

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** projection, shape
**Decision:** A `.docx` with one `Heading 1` and one body paragraph
projects to a non-empty `ProjectionResult` with non-empty `text`, exactly
one heading, a 64-character SHA-256 `content_hash`, `title` resolved
from the filename stem (no `Title`-styled paragraph present), and
`adapter_version == DocxAdapter.VERSION`.

**Precondition:** A `.docx` constructed with one paragraph styled
`Heading 1` ("Introduction") and one plain paragraph.

**Input:** `adapter.project(path)`.

**Expected:**
- `result.text` is a non-empty `str`.
- `len(result.headings) == 1` and `result.headings[0].text == "Introduction"`.
- `result.content_hash` is a 64-char hex string.
- `result.title == "basic"` (the filename stem).
- `result.adapter_version == DocxAdapter.VERSION`.

**Rationale:** The basic-shape test guarantees that the simplest
non-trivial input produces a fully-populated projection. Failures here
indicate a broken adapter contract rather than a feature-specific bug.

### TEST-SAGE-AD-036: Heading extraction uses heading_style_map config

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** configuration, headings
**Decision:** Callers can map any paragraph style to any heading level by
passing `config={"heading_style_map": {<style_name>: <level>, ...}}`.
The adapter does not hard-code which paragraph styles count as headings.

**Precondition:** A `.docx` with one paragraph styled `Title` and one
plain body paragraph.

**Input:** `adapter.project(path, config={"heading_style_map": {"Title": 1}})`.

**Expected:**
- `len(result.headings) == 1`.
- `result.headings[0].level == 1` and `result.headings[0].text == "My Title"`.

**Rationale:** Report and contract templates use `Title` as the
top-level heading; clinical templates use custom styles like
`USPTO Section`. A configurable map is the only way to handle this
without per-vault adapter forks.

### TEST-SAGE-AD-037: Default heading styles work without explicit config

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** configuration, defaults
**Decision:** When `config` omits `heading_style_map`, the adapter
falls back to `Heading 1` through `Heading 9` mapped to levels 1-9 (the
standard Word style identifiers).

**Precondition:** A `.docx` with paragraphs styled `Heading 1`,
`Heading 2`, `Heading 3` ("Level One", "Level Two", "Level Three").

**Input:** `adapter.project(path)` (no config).

**Expected:**
- `len(result.headings) == 3`.
- Levels are 1, 2, 3 in order.

**Rationale:** Most Word documents use the stock heading styles. The
default keeps the simple case zero-config and surfaces typed levels for
the heading hierarchy logic in subsequent tests.

### TEST-SAGE-AD-038: Heading hierarchy paths use ' > ' separator

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** headings, hierarchy
**Decision:** `HeadingNode.path` for a heading is the breadcrumb of all
its ancestors plus its own text, joined by `" > "`. The root heading's
path is just its own text.

**Precondition:** A `.docx` with `Chapter` (H1), `Section` (H2),
`Subsection` (H3) in order.

**Input:** `adapter.project(path)`.

**Expected:**
- `result.headings[0].path == "Chapter"`.
- `result.headings[1].path == "Chapter > Section"`.
- `result.headings[2].path == "Chapter > Section > Subsection"`.

**Rationale:** Path strings are the durable cross-adapter identifier
used by `read_section`, `read_projection`, and the heading-
prefix retrieval contract (AD-020 through AD-022). The separator must
be stable; downstream consumers split on `" > "`.

### TEST-SAGE-AD-039: Title priority: Title style > filename > key terms

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** title_extraction
**Decision:** Title resolution follows a three-step priority chain:
1. The first paragraph styled `Title` (Word's stock title style).
2. The filename stem, if non-empty after stripping the extension.
3. Stop-word-filtered key terms (up to 6) from the first body paragraph
   when neither of the above produces a usable title (filename is
   degenerate, e.g., `.docx` with no stem).

**Precondition:** Three fixture cases:
- Case 1: `.docx` named `AuthoritativeAccumulator.docx` containing a
  `Title`-styled paragraph "Formal Document Title" and an `H1`
  "System Architecture".
- Case 2: `.docx` named `AuthoritativeAccumulator.docx` with `H1`
  "Introduction" and a body paragraph (no `Title` style).
- Case 3: `.docx` named `.docx` (degenerate filename) with `H1`
  "Abstract" and a body paragraph mentioning "authoritative
  accumulator", "clinical normalization", and stop words.

**Input:** `adapter.project(path)` for each case.

**Expected:**
- Case 1: `result.title == "Formal Document Title"`.
- Case 2: `result.title == "AuthoritativeAccumulator"`.
- Case 3: `"authoritative" in result.title.lower()` and `"the"` is not
  among the lower-cased title's whitespace-split tokens.

**Rationale:** Report documents use the `Title` paragraph style for
their actual title; `H1` is typically a generic section heading
("System Architecture", "Introduction") that misleads search. The
key-terms fallback is the last-resort path for the rare case where
neither a `Title` style nor a meaningful filename stem is available.

### TEST-SAGE-AD-040: content_hash is SHA-256 of raw .docx bytes

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** identity, provenance
**Decision:** `result.content_hash` is the SHA-256 hex digest of the
raw `.docx` file bytes (the OPC ZIP package), not of the extracted
text or any normalized form.

**Precondition:** A `.docx` with arbitrary content saved to disk.

**Input:** Compute `hashlib.sha256(path.read_bytes()).hexdigest()`,
then call `adapter.project(path)`.

**Expected:** `result.content_hash` equals the expected hash.

**Rationale:** Content-hash equality drives the duplicate-ingest gate
(BH-133). Hashing raw bytes makes two ingests of the same physical
file equivalent; hashing extracted text would treat reformatted
duplicates as distinct, which is the wrong answer for change
detection.

### TEST-SAGE-AD-041: source_modified_at extracted from file mtime

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** provenance
**Decision:** `result.metadata["source_modified_at"]` is an ISO 8601
timezone-aware string derived from `path.stat().st_mtime`, parsed via
`datetime.fromisoformat`.

**Precondition:** A `.docx` whose mtime is set to a known UTC datetime
(e.g., 2023-06-15 12:00:00 UTC) via `os.utime`.

**Input:** `adapter.project(path)`.

**Expected:**
- `"source_modified_at"` is a key in `result.metadata`.
- `datetime.fromisoformat(result.metadata["source_modified_at"])`
  parses successfully and has a non-None `tzinfo`.
- The parsed datetime is within one second of the known mtime.

**Rationale:** Source-modified provenance is set by every file-based
adapter so the ingestion service can populate
`Document.source_modified_at` (BH-049 through BH-052). The ISO 8601
string keeps `ProjectionResult.metadata` JSON-serializable.

### TEST-SAGE-AD-042: Table content extracted as pipe-delimited text rows

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** projection, tables
**Decision:** Word tables are rendered as Markdown-style pipe-delimited
rows in the projected text: `"| cell | cell | ... |"` per row, header
row included.

**Precondition:** A `.docx` with one `H1` "Data Section" and one table
with rows `[["Name", "Value"], ["alpha", "1"], ["beta", "2"]]`.

**Input:** `adapter.project(path)`.

**Expected:**
- `"| Name | Value |"` is in `result.text`.
- `"| alpha | 1 |"` is in `result.text`.
- `"| beta | 2 |"` is in `result.text`.

**Rationale:** Pipe-delimited rows are readable by both BM25 keyword
search (token-level) and the abstraction provider (the format reads as
intentional structure, not as garbled text). Skipping tables would
silently drop the most information-dense parts of report claim charts
and clinical reference tables.

### TEST-SAGE-AD-043: Mixed headings, paragraphs, tables in correct order

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** projection, ordering
**Decision:** Body paragraphs and tables that appear between two
headings attach to the preceding heading's `content`. The text emerges
in document order, not in any reordered or grouped layout.

**Precondition:** A `.docx` with: `H1` "Overview", body "Intro
paragraph.", a 2x2 table, `H2` "Details", body "Detail paragraph."

**Input:** `adapter.project(path)`.

**Expected:**
- `len(result.headings) == 2`; texts are `"Overview"` then `"Details"`.
- `result.headings[0].content` contains both `"| A | B |"` and
  `"Intro paragraph."` (the table and paragraph between H1 and H2
  attach to "Overview").

**Rationale:** Section-level retrieval (`read_section`) and
heading-prefix queries (AD-020) depend on body content being attached
to the right heading. Mis-attribution would surface the wrong
neighborhood to an agent trying to read a specific section.

### TEST-SAGE-AD-044: Empty document produces valid result with filename title

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** edge_case
**Decision:** A `.docx` containing no paragraphs at all projects
successfully: empty headings list, valid content hash, filename-stem
title.

**Precondition:** A `.docx` with no paragraphs, saved to a path whose
stem is `empty_doc`.

**Input:** `adapter.project(path)`.

**Expected:**
- `result.title == "empty_doc"`.
- `result.headings == []`.
- `result.text` is a `str` (may be empty or whitespace).
- `result.content_hash` is a non-empty `str`.

**Rationale:** Empty Word documents are uncommon but real (e.g., a
freshly-saved file that has not been written to). The adapter must
not crash; the empty-text consequence is handled by BH-134
(abstraction_skipped) one stage downstream.

### TEST-SAGE-AD-045: Custom heading_style_map overrides defaults

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** configuration
**Decision:** When `config["heading_style_map"]` is supplied, custom
entries are added to (not replacing) the default mapping. A document
using both `Title` (mapped via config to level 1) and `Heading 1`
(level 1 via default) produces both as level-1 headings.

**Precondition:** A `.docx` with `Title`-styled "Custom Top" and
`Heading 1`-styled "Standard H1".

**Input:** `adapter.project(path, config={"heading_style_map": {"Title": 1}})`.

**Expected:**
- One heading with text "Custom Top" at level 1.
- One heading with text "Standard H1" at level 1.

**Rationale:** Replacing the default map would force callers to repeat
the standard `Heading 1`..`Heading 9` entries every time they wanted
to add one extra style. Additive merge keeps the common case terse.

### TEST-SAGE-AD-046: Non-heading paragraphs appear as content under nearest heading

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** projection, content_attachment
**Decision:** Each body paragraph attaches to the most recently seen
heading. Paragraphs preceding any heading would be unattached (this
fixture exercises only the post-heading case).

**Precondition:** A `.docx` with: `H1` "Section A", body "First body
paragraph.", body "Second body paragraph.", `H1` "Section B", body
"Third body paragraph."

**Input:** `adapter.project(path)`.

**Expected:**
- Both "First body paragraph." and "Second body paragraph." appear in
  `result.headings[0].content`.
- "Third body paragraph." appears in `result.headings[1].content`.
- "First body paragraph." does NOT appear in
  `result.headings[1].content`.

**Rationale:** The "nearest preceding heading" rule is the canonical
attachment rule for Markdown and DocBook; matching it here keeps
section semantics consistent across source formats.

### TEST-SAGE-AD-047: adapter_version matches DocxAdapter.VERSION

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.VERSION)
**Category:** versioning
**Decision:** Every `ProjectionResult` carries
`adapter_version == DocxAdapter.VERSION` (the class constant). The
adapter does not synthesize the version per call.

**Precondition:** Any `.docx`.

**Input:** `adapter.project(path)`.

**Expected:** `result.adapter_version == DocxAdapter.VERSION`.

**Rationale:** Adapter version is recorded on each `Document` so a
later projection-format change can be detected and re-projected
documents identified. Sourcing the value from the class constant
guarantees the adapter cannot lie about its own version.

### TEST-SAGE-AD-048: Decimal heading numbering prepended to heading text

**Artifact:** `sage/source_adapters/docx_adapter.py` (_NumberingEngine)
**Category:** numbering, projection
**Decision:** When a heading paragraph carries a `numPr` referencing
an abstract numbering definition with `numFmt="decimal"`, the rendered
number ("1", "1.1", "2.1.3") is prepended to the heading text with a
single space.

**Precondition:** A `.docx` with a decimal numbering definition (`numId
100`, abstract num with `numFmt=decimal` at ilvls 0..2) and three
headings: `H1` "Introduction" (ilvl 0), `H1` "Background" (ilvl 0),
`H2` "Definitions" (ilvl 1), all referencing `numId 100`.

**Input:** `adapter.project(path)`.

**Expected:**
- `result.headings[0].text == "1 Introduction"`.
- `result.headings[1].text == "2 Background"`.
- `result.headings[2].text == "2.1 Definitions"`.

**Rationale:** Word renders numbered headings with the prefix visible
to the human reader; without it, the projected text loses the
heading's rendered identity. Report claims and clinical procedures
routinely cross-reference "Section 2.1", and the prefix must be
present in the text for those references to resolve via search.

### TEST-SAGE-AD-049: Child counters reset when parent level increments

**Artifact:** `sage/source_adapters/docx_adapter.py` (_NumberingEngine)
**Category:** numbering, counter_state
**Decision:** When the engine sees a heading at a parent level after
having emitted children at a deeper level, the deeper-level counter
resets to zero so the next child starts at `.1` rather than continuing.

**Precondition:** A `.docx` using `numId 100` (decimal numFmt) with
heading sequence: H1 "Part A", H2 "Sub One", H2 "Sub Two", H1 "Part B",
H2 "Sub One Again".

**Input:** `adapter.project(path)`.

**Expected:**
- "1 Part A", "1.1 Sub One", "1.2 Sub Two", "2 Part B", "2.1 Sub One
  Again" (counter reset on the second H1).

**Rationale:** Without reset, "Sub One Again" would render as "2.3"
and break the numbering invariant Word users expect. The reset rule
also makes the projected numbers match what the human reader sees in
the rendered Word document.

### TEST-SAGE-AD-050: upperRoman and lowerLetter numbering formats

**Artifact:** `sage/source_adapters/docx_adapter.py` (_NumberingEngine)
**Category:** numbering, formats
**Decision:** The engine renders `numFmt` values `upperRoman` (I, II,
III, ...) and `lowerLetter` (a, b, c, ...) in addition to `decimal`.
Mixed-format prefixes are joined left-to-right by `.`.

**Precondition:** A `.docx` using `numId 200` whose abstract numbering
has `upperRoman` at ilvl 0, `decimal` at ilvl 1, `lowerLetter` at
ilvl 2. Three headings: `H1` "First Chapter" (ilvl 0), `H2` "First
Section" (ilvl 1), `H3` "First Item" (ilvl 2).

**Input:** `adapter.project(path)`.

**Expected:**
- "I First Chapter", "I.1 First Section", "I.1.a First Item".

**Rationale:** Report applications and legal briefs use Roman/letter
numbering mixed with decimals (the canonical "I.A.1.a" form). The
engine must render every supported format correctly; falling back
to decimal silently would mis-name half the report corpus.

### TEST-SAGE-AD-051: Cross-ref field cached results in text, instructions excluded

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** projection, fields
**Decision:** Cross-reference fields (REF, PAGEREF, etc.) project as
their cached display value (`w:t` element inside the field) and
exclude the field instruction (`w:instrText` element). Inline runs
adjacent to the field appear in the projected text in their original
order.

**Precondition:** A `.docx` paragraph containing: text "See Section ",
a REF field with instruction `" REF _Ref12345 \\r \\h "` and cached
result "1.1", text " for details."

**Input:** `adapter.project(path)`.

**Expected:**
- "See Section 1.1 for details." appears in `result.text`.
- "REF _Ref12345" does NOT appear in `result.text`.
- "instrText" does NOT appear in `result.text`.

**Rationale:** Field instructions are markup, not content; including
them would pollute the projected text with strings like
"REF _Ref12345 \\r \\h" that mean nothing to a reader and degrade
both BM25 search and abstraction quality. The cached display value is
what the human sees when the document is open in Word.

### TEST-SAGE-AD-052: Multi-level numbering with mixed formats (I.2.a)

**Artifact:** `sage/source_adapters/docx_adapter.py` (_NumberingEngine)
**Category:** numbering, integration
**Decision:** A multi-level numbering scheme with multiple chapters,
each with subsections and items, produces correct prefixes throughout
including counter resets across chapter boundaries.

**Precondition:** A `.docx` using `numId 200` (Roman/decimal/letter
mix) with sequence: H1 "Alpha", H2 "Sub Alpha", H3 "Detail A", H3
"Detail B", H1 "Beta", H2 "Sub Beta".

**Input:** `adapter.project(path)`.

**Expected:**
- "I Alpha", "I.1 Sub Alpha", "I.1.a Detail A", "I.1.b Detail B",
  "II Beta", "II.1 Sub Beta".

**Rationale:** Integration test that combines multi-format rendering
(AD-050) with counter reset (AD-049). A regression in either would
produce an incorrect prefix here without necessarily breaking the
narrower individual tests.

---

## 6. Xlsx Source Adapter

Tier 2 behavioral tests for `sage/source_adapters/xlsx_adapter.py`
(XlsxAdapter). The adapter is a lightweight structural-digest projector
for Excel workbooks via openpyxl in read-only mode. Each worksheet
becomes a level-1 `HeadingNode`; heading paths are flat sheet names with
no hierarchy. Per-sheet content is the column header row (pipe-delimited),
a configurable preview of data rows (default 5), and a dimensions line
(`"<n> rows" / "<m> columns"`). Title resolves from the first sheet's
name with fallback to the filename stem when the sheet is named with
the openpyxl default ("Sheet"/"Sheet1").

The adapter is designed for discovery, not full-content search; an agent
that needs full data retrieves the source `.xlsx` programmatically.

### TEST-SAGE-AD-053: Basic projection returns valid ProjectionResult

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** projection, shape
**Decision:** A single-sheet workbook ("Sales") with header row and two
data rows projects to a non-empty result with one level-1 heading,
non-empty text, a 64-character SHA-256 content hash, and
`adapter_version == XlsxAdapter.VERSION`.

**Precondition:** A `.xlsx` with one sheet "Sales" containing rows
`[["Product", "Revenue"], ["Widget", 100], ["Gadget", 200]]`.

**Input:** `adapter.project(path)`.

**Expected:**
- `result.text` is a non-empty `str`.
- `len(result.headings) == 1` with `text == "Sales"` and `level == 1`.
- `result.content_hash` is a 64-char hex string.
- `result.adapter_version == XlsxAdapter.VERSION`.

**Rationale:** Mirrors AD-035 (docx) for the xlsx adapter: the basic
shape contract must hold for the simplest non-trivial input.

### TEST-SAGE-AD-054: Multiple sheets produce one level-1 HeadingNode each

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** projection, multisheet
**Decision:** A workbook with N sheets produces N `HeadingNode`s in
sheet order, each at level 1.

**Precondition:** A `.xlsx` with three sheets ("Revenue", "Expenses",
"Summary"), each non-empty.

**Input:** `adapter.project(path)`.

**Expected:**
- `len(result.headings) == 3`.
- Heading texts in order: `["Revenue", "Expenses", "Summary"]`.
- Every heading has `level == 1`.

**Rationale:** Sheets are siblings, not nested. A spreadsheet's
organizational structure is laid out across tabs, and the projected
heading tree must reflect that flat layout for downstream
heading-prefix retrieval to work.

### TEST-SAGE-AD-055: Heading paths are sheet names only (no hierarchy)

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** headings, hierarchy
**Decision:** `HeadingNode.path` for an xlsx heading equals the sheet
name verbatim. The `" > "` separator (used by the docx adapter for
nested headings) does not appear in xlsx heading paths.

**Precondition:** A `.xlsx` with sheets "Alpha" and "Beta", each
non-empty.

**Input:** `adapter.project(path)`.

**Expected:**
- `result.headings[0].path == "Alpha"`.
- `result.headings[1].path == "Beta"`.
- `" > "` is not a substring of either path.

**Rationale:** Heading paths are the durable retrieval-side identifier
(see AD-038). For xlsx, the absence of nesting in the path is itself
a signal: agents reading sheet content via `read_section` know
the path is the sheet name.

### TEST-SAGE-AD-056: First row rendered as pipe-delimited header row

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** projection, headers
**Decision:** The first row of every sheet is treated as a header row
and rendered as a pipe-delimited Markdown-style row in the heading's
content.

**Precondition:** A `.xlsx` with one sheet "Data" whose first row is
`["Name", "Age", "City"]`.

**Input:** `adapter.project(path)`.

**Expected:** `"| Name | Age | City |"` is in `result.headings[0].content`.

**Rationale:** Header-row text carries the schema signal that a
discovery query needs ("which sheet has a `City` column?"). Rendering
it as a Markdown row matches the docx table format (AD-042) so a
single retrieval consumer handles both.

### TEST-SAGE-AD-057: Default config includes first 5 data rows, omits row 6+

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** projection, preview
**Decision:** With no config, the adapter projects the first 5 data
rows (rows 2-6) as preview content and omits subsequent rows. The
default of 5 balances signal against projection size.

**Precondition:** A `.xlsx` with one sheet "Data" containing 8 data
rows after the header (`val_1` through `val_8`).

**Input:** `adapter.project(path)`.

**Expected:**
- `val_1` through `val_5` appear in the heading content.
- `val_6`, `val_7`, `val_8` do not appear.

**Rationale:** Spreadsheets can have tens of thousands of rows.
Projecting them all defeats the digest goal and overwhelms BM25 with
data noise. The preview captures schema and a representative sample;
agents who need full data retrieve the source file.

### TEST-SAGE-AD-058: preview_rows config limits data rows per sheet

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** configuration, preview
**Decision:** `config={"preview_rows": N}` overrides the default of 5,
projecting exactly N data rows per sheet.

**Precondition:** Same fixture as AD-057.

**Input:** `adapter.project(path, config={"preview_rows": 2})`.

**Expected:**
- `val_1` and `val_2` appear in the heading content.
- `val_3` does not.

**Rationale:** Vault operators can tune preview depth per workbook
type — clinical reference tables that fit in 20 rows might warrant
the full content; transaction logs warrant a tight preview.

### TEST-SAGE-AD-059: Content includes dimensions line for each sheet

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** projection, metadata
**Decision:** Each sheet's projected content includes a dimensions
line listing the row count (header included) and column count, in
the form `"<N> rows"` / `"<M> columns"`.

**Precondition:** A `.xlsx` with one sheet "Big" containing 1 header
row + 20 data rows × 3 columns (21 rows total, 3 columns).

**Input:** `adapter.project(path)`.

**Expected:** `"21 rows"` and `"3 columns"` both appear in
`result.headings[0].content`.

**Rationale:** Dimensions tell the retrieval consumer "how big is this
sheet?" without forcing a full re-read. An agent making a routing
decision (preview vs. fetch the file) can act on the digest alone.

### TEST-SAGE-AD-060: Title extracted from first sheet name

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** title_extraction
**Decision:** When the first sheet has a non-default name, that name
becomes the projection's `title`.

**Precondition:** A `.xlsx` named `report.xlsx` with first sheet
"Quarterly Report" and second sheet "Details".

**Input:** `adapter.project(path)`.

**Expected:** `result.title == "Quarterly Report"`.

**Rationale:** Sheet names in real workbooks usually carry the
spreadsheet's purpose ("Quarterly Report", "Patient Roster") more
directly than the filename. Preferring the first sheet name as title
matches what a human opening the file would call it.

### TEST-SAGE-AD-061: Default sheet name falls back to filename stem

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** title_extraction, fallback
**Decision:** When the first sheet has openpyxl's default name
("Sheet" or "Sheet1"), the adapter falls back to the filename stem.
A default sheet name is taken as a signal that the author did not
choose a name, not as a meaningful title.

**Precondition:** A `.xlsx` named `my_data.xlsx` whose only sheet has
the default name "Sheet".

**Input:** `adapter.project(path)`.

**Expected:** `result.title == "my_data"`.

**Rationale:** "Sheet" is a placeholder, not a title. Using it would
flood the title field with the same noise word for every freshly-saved
workbook. The filename stem is the next-best signal.

### TEST-SAGE-AD-062: content_hash is SHA-256 of raw .xlsx file bytes

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** identity, provenance
**Decision:** `result.content_hash` is the SHA-256 hex digest of the
raw `.xlsx` bytes (the OPC ZIP package), not of the projected text or
any normalized form.

**Precondition:** A `.xlsx` with arbitrary content saved to disk.

**Input:** Compute `hashlib.sha256(path.read_bytes()).hexdigest()`,
then call `adapter.project(path)`.

**Expected:** `result.content_hash` equals the expected hash.

**Rationale:** Mirrors AD-040 (docx) and AD-078 (pdf). Hashing raw
bytes is the cross-adapter convention; reformatted re-saves produce
new hashes (reflecting actual content change), and identical files
produce identical hashes regardless of when they were ingested.

### TEST-SAGE-AD-063: source_modified_at extracted from file mtime, timezone-aware

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** provenance
**Decision:** `result.metadata["source_modified_at"]` is an ISO 8601
timezone-aware string derived from `path.stat().st_mtime`.

**Precondition:** A `.xlsx` whose mtime is set to a known UTC datetime
(e.g., 2023-09-01 14:00:00 UTC).

**Input:** `adapter.project(path)`.

**Expected:**
- `"source_modified_at"` is a key in `result.metadata`.
- The string parses via `datetime.fromisoformat` to a tz-aware
  datetime within one second of the known mtime.

**Rationale:** Mirrors AD-041 (docx). All file-based adapters share
the same provenance contract.

### TEST-SAGE-AD-064: Empty workbook produces valid result with filename-stem title

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** edge_case
**Decision:** A workbook with no data (only the default empty "Sheet")
projects successfully: filename-stem title, valid content hash, no
crash.

**Precondition:** A `.xlsx` named `empty_book.xlsx` containing only
the default empty "Sheet".

**Input:** `adapter.project(path)`.

**Expected:**
- `result.title == "empty_book"`.
- `result.text` is a `str` (may be empty).
- `result.content_hash` is a 64-char hex string.

**Rationale:** Mirrors AD-044 (docx empty document). The empty-text
abstraction path is the same (BH-134 covers
`abstraction_skipped` transition).

### TEST-SAGE-AD-065: max_sheets config limits number of sheets projected

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** configuration, projection
**Decision:** `config={"max_sheets": N}` truncates projection to the
first N sheets in workbook order.

**Precondition:** A `.xlsx` with 5 sheets named `Sheet_1`..`Sheet_5`.

**Input:** `adapter.project(path, config={"max_sheets": 2})`.

**Expected:**
- `len(result.headings) == 2`.
- `result.headings[0].text == "Sheet_1"`.
- `result.headings[1].text == "Sheet_2"`.

**Rationale:** Some workbooks aggregate hundreds of sheets (one per
day, per patient, per case). The default ingests all sheets, but
operators can cap the projection if those workbooks would otherwise
dominate the index without adding distinct retrieval signal.

### TEST-SAGE-AD-066: metadata includes sheet_names, total_sheets, dimensions

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** metadata
**Decision:** `result.metadata` contains:
- `sheet_names`: list of sheet names in workbook order
- `total_sheets`: integer count
- `dimensions`: dict mapping each sheet name to its dimensions
  (rows × columns)

**Precondition:** A `.xlsx` with two sheets: "Alpha" (3 rows × 2
columns), "Beta" (2 rows × 1 column).

**Input:** `adapter.project(path)`.

**Expected:**
- `result.metadata["sheet_names"] == ["Alpha", "Beta"]`.
- `result.metadata["total_sheets"] == 2`.
- `"Alpha"` and `"Beta"` are keys in `result.metadata["dimensions"]`.

**Rationale:** Sheet inventory is the workbook's structural fingerprint.
A consumer that does not need full content can filter or route on the
sheet names alone (e.g., "all workbooks containing a `Demographics`
sheet").

### TEST-SAGE-AD-067: result.text concatenates all sheet projections with markdown headings

**Artifact:** `sage/source_adapters/xlsx_adapter.py` (XlsxAdapter.project)
**Category:** projection, full_text
**Decision:** `result.text` is the concatenation of all sheet
projections, each prefixed by a Markdown level-1 heading
(`# <sheet_name>`). This is the input for the abstraction stage.

**Precondition:** A `.xlsx` with sheets "Revenue" and "Costs", each
with header and one data row.

**Input:** `adapter.project(path)`.

**Expected:**
- `"# Revenue"` is in `result.text`.
- `"# Costs"` is in `result.text`.
- Cell values from both sheets (`100`, `50`) are in `result.text`.

**Rationale:** Markdown headings give the abstraction provider
sheet-level structure to anchor on, and BM25 search picks up sheet
names as tokenizable content. A flat concatenation without separators
would erase the multi-sheet identity of the workbook.

---

## 7. Docx Adapter -- Template (.dotx) Support

Tests for the Word template branch of `DocxAdapter`. Templates share the
WordprocessingML body structure with documents but are stored as `.dotx`
files with a distinct OPC content type. A template's value is its style
surface (which named styles are defined, their types, inheritance chain,
and whether they carry active numbering), not its body text. These tests
validate that the adapter (a) accepts `.dotx` files without error,
(b) emits a structured style inventory on the `.dotx` branch only, and
(c) emits namespaced tags via the `adapter_tags` channel so existing
`search` tag filters can locate templates by defined style.

### TEST-SAGE-AD-068: DocxAdapter registers .dotx as a supported extension

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.EXTENSIONS)
**Category:** registration
**Decision:** `DocxAdapter.EXTENSIONS` contains both `.docx` and `.dotx`.
A single adapter handles both formats because `.dotx` is structurally
identical WordprocessingML -- only the main part's OPC content type
differs. Duplicating the adapter for a 1-3-file use case is not justified.

**Precondition:** None.

**Input:** Inspect `DocxAdapter.EXTENSIONS`.

**Expected:**
- The list contains `".docx"` and `".dotx"` (order not significant).

**Rationale:** Extension registration is how the ingestion pipeline selects
the adapter for a file. A missing `.dotx` entry would route template
files to no adapter or to a generic fallback, either of which defeats
the feature.

### TEST-SAGE-AD-069: .dotx file is parsed successfully

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** format_compatibility
**Decision:** `adapter.project(path_to_dotx)` completes without error and
returns a valid `ProjectionResult`. The adapter handles the OPC
content-type difference internally (e.g., by opening the package via a
temp copy renamed to `.docx`, or by directly invoking the OPC layer).

**Precondition:** A fixture `.dotx` file exists. Construction: create a
`docx.Document` with one `Heading 1`, save to a `.docx` path, then
rewrite the ZIP so the main part's `[Content_Types].xml` entry uses
the template content type
(`application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml`)
and rename the file to `.dotx`.

**Input:** `adapter.project(path_to_dotx)`.

**Expected:**
- Returns a `ProjectionResult` with non-empty `text`.
- `content_hash` is a 64-char hex string (SHA-256 of raw `.dotx` bytes).
- `headings` contains the heading present in the template.
- No exception raised.

**Rationale:** python-docx validates the main part's content type at
`Document()` load time and historically rejected `.dotx`. If the
adapter's workaround is correct, loading succeeds. If it regresses,
every template ingest fails at stage 1.

### TEST-SAGE-AD-070: .dotx projection populates template_style_inventory; .docx does not

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** metadata, template_surface
**Decision:** When the source extension is `.dotx`, the adapter emits
`projection.metadata["template_style_inventory"]` as a list of
structured style entries. When the source extension is `.docx`, this
key is absent (not present-but-empty) so callers can distinguish
"template surface intentionally exposed" from "document with no
templates". Inventory computation is gated on extension, not on
heuristic detection of a template-like document.

**Precondition:** A fixture `.dotx` and a fixture `.docx` with
identical body content (one `Heading 1`, one body paragraph).

**Input:**
1. `adapter.project(path_to_dotx)`
2. `adapter.project(path_to_docx)`

**Expected:**
- Case 1: `result.metadata["template_style_inventory"]` is a non-empty list.
- Case 2: `"template_style_inventory"` is not a key in `result.metadata`.

**Rationale:** Scoping the inventory to `.dotx` keeps document metadata
lean and avoids implying style-surface intent for incidentally
style-rich documents. The inventory becomes promotable to `.docx`
later behind an opt-in flag if the need arises.

### TEST-SAGE-AD-071: Each inventory entry carries required fields

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** metadata, schema_shape
**Decision:** Every entry in `template_style_inventory` is a dict with
exactly these keys: `id` (str, XML style ID), `name` (str, human style
name), `type` (str, one of `paragraph`, `character`, `table`,
`numbering`), `based_on` (str or None, the style ID this inherits
from), `has_numbering` (bool), `is_custom` (bool, True iff the style
element carries `w:customStyle="1"`), `numbering_detail` (dict or
None; see AD-075 for its shape). Additional keys MAY be present only
if introduced by a later spec revision.

**Precondition:** A `.dotx` fixture with at least one style from each
type (paragraph, character, table, numbering), at least one style
whose `basedOn` points to another style, and a mix of built-in and
user-authored (custom) styles.

**Input:** `adapter.project(path_to_dotx)`

**Expected:**
- Every entry is a dict and contains exactly the required keys
  (no extras, no omissions).
- `id` and `name` are non-empty strings.
- `type` is one of the four allowed values.
- `based_on` is a string when inheritance exists, otherwise `None`.
- `has_numbering` is a `bool` (not truthy/falsy non-bool).
- `is_custom` is `True` for user-authored styles and `False` for
  built-in styles the template merely carries without modification.
- `numbering_detail` is a dict when `has_numbering` is True, otherwise
  `None`. The two flags are required to be consistent.

**Rationale:** The inventory is the primary durable query surface for
template selection. A loose shape (keys that vary across entries,
values of unexpected types) would force every downstream consumer to
re-validate, defeating the point of a structured representation.
Locking the shape now prevents silent drift. `is_custom` distinguishes
styles the template author deliberately introduced from the ~20 stock
Word styles that ride along in every template; downstream selection
logic needs this to compare templates meaningfully.

### TEST-SAGE-AD-072: has_numbering reflects active numbering reference

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** metadata, numbering_semantics
**Decision:** For a paragraph style, `has_numbering` is `True` if and
only if the style's `w:pPr > w:numPr > w:numId` resolves (via the
document's numbering part) to an active abstract numbering definition.
A `numId` of `0` or a missing numbering part yields `False`. For
non-paragraph styles, `has_numbering` is always `False`.

**Precondition:** A `.dotx` fixture with three styles:
- `HeadingAutoNum` -- paragraph style with a valid `numPr` pointing at
  an abstract numbering definition.
- `HeadingPlain` -- paragraph style with no `numPr`.
- `AnnotationChar` -- character style.

**Input:** `adapter.project(path_to_dotx)`

**Expected:**
- Entry for `HeadingAutoNum` has `has_numbering == True`.
- Entry for `HeadingPlain` has `has_numbering == False`.
- Entry for `AnnotationChar` has `has_numbering == False`.

**Rationale:** `has_numbering` is the semantic signal that distinguishes
"this heading style auto-numbers" from "this heading style relies on
hand-typed numbers". Templates that mix the two confuse agentic
document construction. The bool must be grounded in XML truth, not in
style name heuristics.

### TEST-SAGE-AD-073: .dotx projection emits adapter_tags; .docx does not

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** metadata, discoverability
**Decision:** When the source extension is `.dotx`, the adapter emits
`projection.metadata["adapter_tags"]` as a list of strings. The list
contains:
- `template:style:<style_name>` for every entry whose
  `is_custom == True`. Built-in styles are excluded here to prevent
  every template from answering yes to queries about stock Word
  styles (`Normal`, `Header`, `Footer`, etc.).
- `template:has_numbering:<style_name>` for every entry whose
  `has_numbering == True`, regardless of `is_custom`. Built-in
  heading styles frequently carry template-local auto-numbering
  wiring, and that wiring is a meaningful discriminator between
  templates; restricting this tag to custom styles would hide it.

When the source extension is `.docx`, `adapter_tags` is absent from
`result.metadata`.

**Precondition:** A `.dotx` fixture with:
- custom paragraph style `AppendixHeading` with no numbering
- custom paragraph style `DefinitionsHeader` with active numbering
- built-in style `Heading 1` with active numbering (template-local
  modification of the stock style)
- built-in style `Normal` unmodified

A `.docx` fixture with equivalent body content.

**Input:**
1. `adapter.project(path_to_dotx)`
2. `adapter.project(path_to_docx)`

**Expected:**
- Case 1: `"adapter_tags"` is present; contains
  `template:style:AppendixHeading`,
  `template:style:DefinitionsHeader`,
  `template:has_numbering:DefinitionsHeader`, and
  `template:has_numbering:Heading 1`. Does NOT contain
  `template:style:Heading 1` or `template:style:Normal` (built-in
  styles excluded from the style-presence namespace).
- Case 2: `"adapter_tags"` is not a key in `result.metadata`.

**Rationale:** `adapter_tags` is the channel by which the adapter
contributes to `document.tags` (see BH-131). The `template:` namespace
prefix prevents collision with caller-supplied or filename-parsed tags
and lets `search` queries filter cleanly. The asymmetry between
the style-presence and has-numbering tag sets (custom-only vs.
all-styles) reflects empirical observation: stock Word styles carry
little selection signal by presence alone, but any style carrying
template-local numbering wiring is a signal worth exposing.

### TEST-SAGE-AD-074: .dotx projection produces non-empty text derived from style inventory

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** projection, template_surface
**Decision:** When the source extension is `.dotx`, the adapter
synthesizes a descriptive prose projection from the style inventory so
that:
- The projection is not empty (abstraction does not crash; BH-134
  covers the defensive fallback for other empty-text cases).
- The template is findable via BM25 keyword search on its custom style
  names.
- The template's semantic embedding reflects what the template is for
  rather than the near-zero body content that a raw parse produces.

The synthesized text lists the custom-authored styles with their type,
calls out built-in styles carrying template-local auto-numbering, and
opens with a line identifying the file as a Word template with the
document's title. `.docx` projections are unchanged.

**Precondition:** A `.dotx` fixture containing:
- Several custom paragraph styles (e.g., `USPTO Section`, `Definition`)
- One custom character style (e.g., `Annotation Char`)
- Built-in `Heading 1` wired to active numbering
- No body content (simulating a real template).

**Input:** `adapter.project(path_to_dotx)`

**Expected:**
- `result.text.strip()` is non-empty.
- The first line identifies the artifact as a Word template and
  includes the title (filename stem or Title-styled paragraph).
- The text contains every custom style's human name.
- The text mentions that `Heading 1` carries template-local
  auto-numbering (or an equivalent phrasing).
- The text does NOT contain style IDs from built-in styles that the
  template does not modify (e.g., `Normal`, `Header`, `Footer`) --
  mentioning them would flood every template's text with identical
  keyword noise.

**Negative:** For a `.docx` fixture with identical body content as
the projection function would otherwise extract, `result.text` is
unchanged from v0.1.0 behavior (no template-style-surface synthesis).

**Rationale:** A template's value to an agentic workflow is its style
surface, not its body. If the projected text is empty or near-empty,
vector and BM25 retrieval cannot surface the template, and the
abstraction provider's strict-quality gate blocks the pipeline. A
synthesized description of the style surface gives retrieval
something meaningful to index on and gives the abstraction provider
a substantive input.

### TEST-SAGE-AD-075: numbering_detail carries the resolved numbering definition

**Artifact:** `sage/source_adapters/docx_adapter.py` (DocxAdapter.project)
**Category:** metadata, numbering_semantics
**Decision:** For every inventory entry where `has_numbering` is True,
the `numbering_detail` field is a dict with exactly these keys:
- `num_id` (int): the `w:numId` the style's `numPr` references.
- `abstract_num_id` (int): the `w:abstractNumId` that `num_id` resolves
  to via the document's numbering part.
- `ilvl` (int): the `w:ilvl` the style's `numPr` references (typically
  0 for a heading-style reference).
- `num_fmt` (str): the `w:numFmt` value at that `ilvl` in the resolved
  abstract (e.g., `"decimal"`, `"upperRoman"`, `"lowerLetter"`,
  `"none"`).
- `lvl_text` (str): the `w:lvlText` template at that `ilvl`, exactly as
  authored (e.g., `"%1."`, `"%1.%2"`, `"Section %1:"`).
- `suppressed` (bool): True when a `<w:lvlOverride>` inside the
  `<w:num>` sets `numFmt` to `"none"` at the matching `ilvl` (i.e.,
  the style carries numbering wiring but the rendered output will have
  no number). False otherwise.

When `has_numbering` is False, `numbering_detail` is `None`. The two
keys are required to be internally consistent.

**Precondition:** A `.dotx` fixture with:
- `DecimalHeading`: custom paragraph, numId→abstractNum with
  `numFmt=decimal` and `lvlText="%1."` at ilvl 0.
- `RomanSubheading`: custom paragraph, numId→abstractNum with
  `numFmt=upperRoman` and `lvlText="%1.%2"` at ilvl 1.
- `SuppressedHeading`: custom paragraph whose num carries a
  `<w:lvlOverride w:ilvl="0"><w:lvl><w:numFmt w:val="none"/></w:lvl></w:lvlOverride>`.
- `PlainBody`: custom paragraph with no numbering.

**Input:** `adapter.project(path_to_dotx)`

**Expected:**
- `DecimalHeading`: `numbering_detail == {"num_id": N, "abstract_num_id": M, "ilvl": 0, "num_fmt": "decimal", "lvl_text": "%1.", "suppressed": False}` for some concrete integer IDs.
- `RomanSubheading`: `num_fmt == "upperRoman"`, `lvl_text == "%1.%2"`, `ilvl == 1`, `suppressed == False`.
- `SuppressedHeading`: `has_numbering == True`, `suppressed == True`.
- `PlainBody`: `has_numbering == False`, `numbering_detail is None`.

**Rationale:** A boolean `has_numbering` answers *whether* a style is
numbered but not *how*. An agentic preflight step that generates
a document from a selected template needs to verify not just that
`AppendixHeading` exists but that it is wired to produce the expected
format — e.g., that Appendix A/B/C comes out as "A" rather than "1" or
that an intentionally-suppressed style will not render a visible
number. The concrete `num_fmt`, `lvl_text`, and `suppressed` fields
carry that verification signal. Keeping `has_numbering` alongside as
a fast-path bool means filter queries do not need to check
`numbering_detail is not None` on every entry.

---

## 8. PDF Source Adapter

Tier 2 behavioral tests for `sage/source_adapters/pdf_adapter.py`
(PdfAdapter v0.1). Native-text-only scope: PDFs with a real text layer
are projected directly via `pdfplumber`; image-only ("scanned") PDFs are
detected and surfaced via an `adapter_tags` signal so the user can
re-OCR them externally and re-ingest. OCR inside SAGE is out of scope
for v0.1.

Programmatic test fixtures generated via `reportlab` (text + outlined
PDFs) and `Pillow` (image-only "scanned" PDFs); `reportlab` and
`Pillow` are test-only dependencies.

### Section 8.1 — Registration & basic projection

### TEST-SAGE-AD-076: PdfAdapter registers `.pdf` as a supported extension

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter)
**Category:** registration
**Decision:** `PdfAdapter.EXTENSIONS == [".pdf"]`. The scan service derives
its file-extension-to-adapter map from each adapter's `EXTENSIONS`
class attribute, so a missing or misspelled value silently disables the
adapter for the scan UI.

**Precondition:** None (pure attribute check).

**Input:** Inspect `PdfAdapter.EXTENSIONS`.

**Expected:**
- `PdfAdapter.EXTENSIONS == [".pdf"]`
- `PdfAdapter.VERSION` is a non-empty string matching `r"^\d+\.\d+\.\d+$"`

**Rationale:** Mirrors AD-068 for the docx adapter's `.dotx` registration.
Encodes the contract between adapter and the scan service's auto-derived
extension map.

### TEST-SAGE-AD-077: Native-text projection extracts text and produces a flat single-section heading when no outline is present

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** projection, headings
**Decision:** A PDF with a real text layer and no `/Outlines` produces a
`ProjectionResult` whose `text` carries the full extracted body, and
whose `headings` list contains exactly one `HeadingNode` at level 1
covering the whole document.

**Precondition:** A programmatic single-page PDF generated via
`reportlab`, containing one paragraph of recognizable English text and
no outline entries.

**Input:** `await adapter.project(source_path)`

**Expected:**
- `result.text` contains the paragraph's recognizable substring.
- `len(result.headings) == 1`
- `result.headings[0].level == 1`
- `result.headings[0].path == result.title`
- `result.headings[0].content` equals `result.text` (modulo whitespace).

**Rationale:** v0.1 deliberately defers font-size heuristics. When the
PDF carries no outline, the adapter does not invent structure; it
exposes the document as a single flat section so that retrieval still
gets indexable chunks under a coherent `heading_path`. Most
machine-generated PDFs (legal reports, exported letters, single-section
memos) fall into this case.

### TEST-SAGE-AD-078: `content_hash` is SHA-256 of raw source bytes

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** provenance, dedup
**Decision:** `result.content_hash` is the lowercase hex SHA-256 of the
source file's raw bytes, identical to other adapters.

**Precondition:** Any readable PDF fixture.

**Input:** `await adapter.project(source_path)`; independently compute
`hashlib.sha256(source_path.read_bytes()).hexdigest()`.

**Expected:**
- `result.content_hash == hashlib.sha256(source_path.read_bytes()).hexdigest()`
- All-lowercase, 64 hex characters.

**Rationale:** The ingestion service uses `content_hash` for the dedup
gate. A divergent hashing scheme (e.g., hashing the projected text
instead of the source bytes) would break dedup across re-ingest and
break the contract with other adapters. Mirrors AD-067 (xlsx) and the
markdown adapter's hashing.

### Section 8.2 — Provenance & metadata

### TEST-SAGE-AD-079: `source_modified_at` extracted from file mtime

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** provenance, metadata
**Decision:** The PDF adapter calls `source_path.stat()` to extract the
file's `st_mtime` and includes it in `ProjectionResult.metadata` as an
ISO 8601 UTC string keyed `"source_modified_at"`.

**Precondition:** Programmatic PDF written to a tmp path; `os.utime` set
to a known instant for determinism.

**Input:** `await adapter.project(source_path)`

**Expected:**
- `result.metadata["source_modified_at"]` is a string.
- `datetime.fromisoformat(...)` parses it successfully.
- Parsed datetime is timezone-aware (UTC).
- Parsed datetime matches the set mtime within 1-second tolerance.

**Rationale:** Mirrors AD-034 (markdown). File-based adapters are the
natural extraction point for filesystem provenance; the document's
`source_modified_at` column is populated by the ingestion service from
this metadata key.

### TEST-SAGE-AD-080: `page_count` metadata reflects the actual page count of the source

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** metadata
**Decision:** `result.metadata["page_count"]` is the integer page count
of the source PDF, regardless of how many pages were projected. When
truncation occurs (see AD-094) `page_count` continues to report the
source's actual count; `pages_extracted` reports what was projected.

**Precondition:** Programmatic 7-page PDF.

**Input:** `await adapter.project(source_path)`

**Expected:**
- `result.metadata["page_count"] == 7`
- `result.metadata["pages_extracted"] == 7` (no truncation here).
- `"pdf:truncated"` not in `result.metadata.get("adapter_tags", [])`.

**Rationale:** `page_count` describes the source artifact; truncation is
a separate signal (AD-094). Overloading one field would lose the delta
between "what's in the file" and "what the adapter chose to project,"
which becomes valuable when re-ingesting after raising the
`max_pages` config.

### Section 8.3 — Title priority chain

### TEST-SAGE-AD-081: Title resolves from `/Info /Title` when present (priority 1)

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** title, metadata
**Decision:** When the PDF's `/Info` dictionary contains a non-empty
`/Title` entry, the adapter returns that string as `result.title`,
overriding all lower-priority sources.

**Precondition:** Programmatic PDF with `/Info /Title = "Set Via Info"`,
a different first outline entry "Outline Heading 1", a different first
body line "First Line Title", and a non-trivial filename stem.

**Input:** `await adapter.project(source_path)`

**Expected:**
- `result.title == "Set Via Info"`

**Rationale:** Authors can set `/Info /Title` deliberately (Word's
Document Properties, LaTeX `\title{}`, PDF export dialogs). When set,
it represents the most authoritative title intent. Mirrors the
DocxAdapter pattern of preferring the most-explicit title source.

### TEST-SAGE-AD-082: Title falls back to first outline entry when `/Info /Title` absent (priority 2)

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** title, metadata
**Decision:** When `/Info /Title` is absent or empty and the PDF has an
outline, the first outline entry's text becomes `result.title`.

**Precondition:** Programmatic PDF with empty `/Info` and an outline
whose first entry is "Outline Heading 1".

**Input:** `await adapter.project(source_path)`

**Expected:**
- `result.title == "Outline Heading 1"`

**Rationale:** Outline entries are author-curated structural markers,
more reliable than body-line guessing. When `/Info` is silent, the
outline is the next-best author intent.

### TEST-SAGE-AD-083: Title falls back to first body line ≤120 chars when no `/Info` and no outline (priority 3)

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** title, heuristic
**Decision:** When `/Info /Title` is absent and the PDF has no outline,
the adapter scans the projected body text and returns the first
non-empty line whose length is ≤120 characters as `result.title`. Empty
or whitespace-only leading lines are skipped.

**Precondition:** Programmatic PDF with empty `/Info`, no outline, and
body text whose first non-empty line is "First Line Title" (well under
120 chars), followed by additional content.

**Input:** `await adapter.project(source_path)`

**Expected:**
- `result.title == "First Line Title"`

**Rationale:** A short leading line is overwhelmingly likely to be a
human-readable title. The 120-char cap rejects accidental title
detection on cover pages where the first line is a paragraph of
boilerplate. Mirrors the DocxAdapter `_extract_key_terms` fallback
intent.

### TEST-SAGE-AD-084: Title falls back to filename stem as last resort (priority 4)

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** title, fallback
**Decision:** When `/Info /Title` is absent, no outline exists, and no
body line ≤120 chars is found, `result.title` is `source_path.stem`.

**Precondition:** Programmatic PDF with empty `/Info`, no outline, and
a body whose first line exceeds 120 characters; saved as
`tmp/long-leading-line.pdf`.

**Input:** `await adapter.project(source_path)`

**Expected:**
- `result.title == "long-leading-line"`

**Rationale:** Filename is the last resort signal. It will at least be
human-typed (not adapter-guessed) and consistent with how the user
already organizes the file. Empty-string titles are not acceptable
because downstream code uses `title` as the root `heading_path`.

### Section 8.4 — Outline handling

### TEST-SAGE-AD-085: Outlined PDF produces HeadingNodes mirroring outline structure

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** projection, headings
**Decision:** When the PDF has an outline, each outline entry becomes a
`HeadingNode` whose `level` matches the outline depth (root = 1),
`text` is the outline label, `path` is `" > ".join(ancestors + [text])`,
and `content` is the text extracted from the page range from this
entry's destination page up to (but not including) the next sibling or
parent's next-sibling destination page.

**Precondition:** Programmatic PDF with an outline:
- Level 1: "Intro" (page 1)
  - Level 2: "Background" (page 2)
    - Level 3: "Prior Art" (page 3)

Each section contains a paragraph whose text uniquely identifies it
(e.g., "INTRO_BODY", "BACKGROUND_BODY", "PRIOR_ART_BODY").

**Input:** `await adapter.project(source_path)`

**Expected:**
- `len(result.headings) == 3`
- `result.headings[0] == HeadingNode(level=1, text="Intro", path="Intro", content=...)` where content contains "INTRO_BODY".
- `result.headings[1].path == "Intro > Background"`; content contains "BACKGROUND_BODY".
- `result.headings[2].path == "Intro > Background > Prior Art"`; content contains "PRIOR_ART_BODY".

**Rationale:** Outline-derived headings give retrieval the same
structural anchoring that docx and markdown adapters provide via their
native heading constructs. The `path` construction matches the
HeadingNode docstring's example (`"Section 3 > Definitions >
Normalization"`) so downstream chunk metadata is consistent across
adapters.

### TEST-SAGE-AD-086: `has_outline=True` and `pdf:has_outline` adapter tag emitted when outline present

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** metadata, tags
**Decision:** When the PDF has a non-empty outline, the adapter sets
`metadata["has_outline"] = True` and includes `"pdf:has_outline"` in
`metadata["adapter_tags"]`. When no outline is present,
`metadata["has_outline"] = False` and no `pdf:has_outline` tag is
emitted.

**Precondition:** Two fixtures — one with an outline, one without.

**Input:** `await adapter.project(source_path)` for each.

**Expected:**
- Outlined fixture: `result.metadata["has_outline"] is True`;
  `"pdf:has_outline" in result.metadata["adapter_tags"]`.
- No-outline fixture: `result.metadata["has_outline"] is False`;
  `"pdf:has_outline"` not in `result.metadata.get("adapter_tags", [])`.

**Rationale:** Both the boolean and the tag carry the same signal in
two channels. The boolean is structured and queryable; the tag flows
into `document.tags` (via `adapter_tags` plumbing — BH-131) and is
filterable in the discover/search UI without joining metadata. Mirrors
AD-073's tag-emission contract for the docx adapter.

### TEST-SAGE-AD-087: Outline depth cap (10) drops deeper entries; their text remains accessible via the level-10 ancestor's content

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** safety, headings
**Decision:** Outline entries whose depth exceeds 10 are dropped from
`result.headings`. Their underlying body text is still projected: by
construction, the page range of the nearest level-≤10 ancestor covers
the dropped entry's pages, so the dropped entry's body text appears in
that ancestor's `content`.

**Precondition:** Programmatic PDF with a synthetic 12-level deep
outline. Each level's heading text is unique
(`"Level1"`..`"Level12"`); each level's body contains a unique marker
(`"BODY_L1"`..`"BODY_L12"`).

**Input:** `await adapter.project(source_path)`

**Expected:**
- `max(node.level for node in result.headings) == 10`
- No `HeadingNode` exists with `text == "Level11"` or
  `text == "Level12"`.
- The level-10 `HeadingNode`'s `content` contains `"BODY_L11"` and
  `"BODY_L12"` (their text is preserved within the ancestor's page
  range).

**Rationale:** Real-world outlines are rarely deeper than four levels;
the cap exists purely as a safety bound against pathological or
adversarial PDFs. Dropping (rather than collapsing into ancestors as
explicit subheadings) preserves structural fidelity at the visible
levels. Text loss is not a concern because page-range extraction
inherently covers descendant page ranges.

### Section 8.5 — Scanned-PDF detection

### TEST-SAGE-AD-088: Scanned-only PDF produces empty projection with `pdf:scanned` tag

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** detection, tags
**Decision:** A PDF whose pages collectively yield zero non-whitespace
extracted characters (`total_chars == 0` after stripping each page) is
treated as scanned. The adapter returns `result.text == ""`,
`result.headings == []`, `metadata["adapter_tags"]` containing
`"pdf:scanned"`, and `metadata["adapter_tag_prefixes"]` containing
`"pdf:"`. The title is still resolved via the priority chain
(typically falling through to filename stem since `/Info /Title`,
outline, and body lines are all absent in image-only PDFs).

The simpler "any text at all" threshold (vs. the originally-considered
"chars/page < 50 AND total < 200") trades sensitivity for predictability:
real-world scanned PDFs that happen to contain a few stray text-layer
artifacts (page numbers, headers rendered as text alongside images) are
not flagged, but neither are tiny native-text PDFs ever falsely flagged
as scanned. False negatives are recoverable (the user notices an empty
abstract and re-OCRs externally); false positives are not (the document
gets indexed empty and silently disappears from search).

**Precondition:** Programmatic single-page PDF generated by rendering
the string "This image carries no text layer." into a PNG via Pillow,
then embedding the PNG into a PDF page with no text drawing
operations. The resulting PDF has zero extractable text.

**Input:** `await adapter.project(source_path)`

**Expected:**
- `result.text == ""`
- `result.headings == []`
- `"pdf:scanned" in result.metadata["adapter_tags"]`
- `"pdf:" in result.metadata["adapter_tag_prefixes"]`
- `result.title == source_path.stem` (filename fallback applies).
- No exception is raised.

**Rationale:** Rather than failing or attempting in-process OCR, v0.1
flags scanned PDFs and routes them through the empty-projection
pathway, which the ingestion service transitions to
`abstraction_skipped` (consistent with BH-134's empty-docx behavior).
The user sees `pdf:scanned` in the scan UI and can re-OCR via Acrobat
externally, then re-ingest. Defers OCR-quality and OCR-dependency
debates to a Phase 2 decision.

### TEST-SAGE-AD-089: `adapter_tag_prefixes` declares `["pdf:"]` whenever a `pdf:` tag is contributed

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** tags, re-ingest
**Decision:** Whenever the adapter contributes any `pdf:`-prefixed
tag in `metadata["adapter_tags"]` (e.g., `pdf:has_outline`,
`pdf:scanned`, `pdf:truncated`), it must also set
`metadata["adapter_tag_prefixes"] = ["pdf:"]` so that on force
re-ingest, stale `pdf:`-prefixed tags are stripped from the
document before fresh ones are applied. When no `pdf:` tag is
contributed, `adapter_tag_prefixes` may be omitted or set to `[]`.

**Precondition:** Two fixtures: an outlined PDF (emits
`pdf:has_outline`) and a scanned PDF (emits `pdf:scanned`).

**Input:** `await adapter.project(source_path)` for each.

**Expected:**
- Outlined fixture: `result.metadata["adapter_tag_prefixes"] == ["pdf:"]`.
- Scanned fixture: `result.metadata["adapter_tag_prefixes"] == ["pdf:"]`.

**Rationale:** Mirrors AD-073 / BH-132's `adapter_tag_prefixes`
contract for the docx adapter. Without this declaration, the ingestion
service has no way to know which `document.tags` are adapter-owned
versus caller-owned, and stale tags accumulate across re-ingests.

### Section 8.6 — Failure modes

### TEST-SAGE-AD-090: Encrypted PDF raises `ValueError`

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** error_handling
**Decision:** When the source PDF is encrypted (owner or user password
set) and the adapter has no decryption credentials, `project()`
raises `ValueError` whose message mentions "encrypted". The adapter
does not attempt to silently produce an empty projection, because the
file genuinely could be readable to a credentialed user; the failure
must be loud.

**Precondition:** Programmatic PDF generated with an owner password
(via `reportlab.pdfgen.canvas` encryption arguments).

**Input:** `await adapter.project(source_path)`

**Expected:**
- `pytest.raises(ValueError)` with message matching `r"encrypted"i`.

**Rationale:** Distinguishes "PDF format we can't currently read" from
"PDF with no text layer." The first is a credential problem the user
might solve; the second is OCR-deferred to Phase 2. Conflating them
would hide encrypted files behind the `pdf:scanned` flag and confuse
diagnosis.

### TEST-SAGE-AD-091: Corrupt PDF raises `ValueError`

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** error_handling
**Decision:** When the source file is not a valid PDF (e.g., truncated,
not a PDF at all, or has unrecoverable structural damage),
`project()` raises `ValueError`.

**Precondition:** Two fixtures:
- A file containing `b"%PDF-1.7\nnot real content"` (bad header
  followed by garbage).
- A file that is genuinely empty (0 bytes).

**Input:** `await adapter.project(source_path)` for each.

**Expected:**
- Both raise `ValueError`.

**Rationale:** The ingestion service catches adapter exceptions and
records them as ingestion failures. Raising rather than returning an
empty projection ensures the failure is surfaced and the document is
not silently indexed with no content.

### TEST-SAGE-AD-092: Malformed-but-readable PDF projects successfully without stderr leakage

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** error_handling, log_hygiene
**Decision:** PDFs with mildly malformed xref tables (the class that
`pdfminer.six` and `pypdf` print "Ignoring wrong pointing object N M
(offset 0)" warnings for) project successfully. The adapter suppresses
these stderr warnings inside `project()` (via a stderr redirect or
`warnings`/`logging` filter scoped to the call) so the SAGE server log
is not flooded.

**Precondition:** A real-world malformed-xref PDF fixture. Either
checked-in (small, license-clean) or generated by a fixture builder
that constructs a PDF with intentionally-broken xref offsets but
still-extractable content streams.

**Input:**
1. Capture pypdf/pdfminer log records via pytest's `caplog.at_level(logging.WARNING, ...)`.
2. `await adapter.project(source_path)`.

**Expected:**
- `result.text` is non-empty (extraction succeeded).
- The captured log records contain no occurrence of "Ignoring
  wrong pointing object" (or the related pypdf malformed-xref messages
  "incorrect startxref" / "parsing for Object Streams"). The adapter's
  suppression sets the relevant loggers to ERROR so WARN records never
  propagate; `caplog` observes the level filter directly, which
  `contextlib.redirect_stderr` cannot do under pytest's logging plugin.

**Rationale:** A real fraction of PDFs in the wild (the PV01
feasibility search report is one) parse cleanly content-wise but
emit a flurry of object-pointer warnings during parsing. Letting these
leak into the SAGE log creates noise that drowns real diagnostic
signal. The adapter is the right place to scope the suppression so
that other PDF tooling outside SAGE remains verbose by default.

### TEST-SAGE-AD-093: Empty (zero-page) PDF produces empty projection without error

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** edge_case
**Decision:** A structurally valid PDF with `/Pages /Count 0` (or
otherwise containing no page objects) produces
`result.text == ""`, `result.headings == []`, and
`result.metadata["page_count"] == 0`. No exception is raised. The
title falls through to filename stem.

**Precondition:** Programmatic zero-page PDF.

**Input:** `await adapter.project(source_path)`

**Expected:**
- `result.text == ""`
- `result.headings == []`
- `result.metadata["page_count"] == 0`
- `result.metadata["pages_extracted"] == 0`
- `result.title == source_path.stem`
- No exception.

**Rationale:** Treats zero-page PDFs the same as empty docx (BH-134):
empty projection routes ingestion to `abstraction_skipped` rather than
crashing the abstraction quality guard. Distinct from the scanned case
(AD-088) because no `pdf:scanned` tag applies — there's no content to
OCR.

### Section 8.7 — Configuration

### TEST-SAGE-AD-094: `max_pages` config: `pages_extracted` and `pdf:truncated` tag distinguish truncation from actual page count

**Artifact:** `sage/source_adapters/pdf_adapter.py` (PdfAdapter.project)
**Category:** config, truncation
**Decision:** When the adapter receives `config={"max_pages": N}` and
the source PDF has more than N pages, only the first N pages
contribute to `result.text` and to outline-derived `HeadingNode`
content. `metadata["page_count"]` continues to report the source's
actual page count; `metadata["pages_extracted"]` reports N; and
`metadata["adapter_tags"]` includes `"pdf:truncated"`. When the PDF
has ≤N pages, `pages_extracted == page_count` and no `pdf:truncated`
tag is emitted.

**Precondition:** Programmatic 10-page PDF with each page containing a
unique marker (`"PAGE_1_BODY"`..`"PAGE_10_BODY"`).

**Input:**
1. `await adapter.project(source_path, config={"max_pages": 3})`
   (truncation case).
2. `await adapter.project(source_path, config={"max_pages": 10})`
   (no-truncation case).
3. `await adapter.project(source_path, config={"max_pages": 100})`
   (limit above actual count).

**Expected:**
- Truncation case (max_pages=3):
  - `result.metadata["page_count"] == 10`
  - `result.metadata["pages_extracted"] == 3`
  - `"pdf:truncated" in result.metadata["adapter_tags"]`
  - `result.text` contains `"PAGE_1_BODY"`, `"PAGE_2_BODY"`,
    `"PAGE_3_BODY"`; does not contain `"PAGE_4_BODY"` or
    `"PAGE_10_BODY"`.
- No-truncation case (max_pages=10):
  - `pages_extracted == page_count == 10`
  - `"pdf:truncated"` not in `adapter_tags`.
- Limit-above case (max_pages=100):
  - `pages_extracted == page_count == 10`
  - `"pdf:truncated"` not in `adapter_tags`.

**Rationale:** Two-field design separates the artifact's ground truth
(`page_count`) from the adapter's behavior (`pages_extracted`). The
`pdf:truncated` tag surfaces the event in `document.tags` so it is
filterable in the discover/search UI. If `max_pages` is later raised
and the document re-ingested, the delta between the two fields makes
the change visible without re-reading the file.

---

## 9. Qwen3 AbstractionProvider -- Lazy Loading

These three tests cover the lazy-loading branch of `Qwen3AbstractionProvider`
(deferred from AD-026's eager-load contract per CAS-ADR-013 / v0.9.0,
2026-04-11). They were originally numbered AD-035..AD-037; renumbered
to AD-095..AD-097 (2026-04-25) to densely pack the source-adapter spec
range AD-035..AD-094 after backfilling docx/xlsx specs.

### TEST-SAGE-AD-095: Lazy loading defers model allocation to first call

**Artifact:** `sage/adapters/abstraction_qwen3.py` (Qwen3AbstractionProvider)
**Category:** initialization, memory
**Decision:** Constructor stores configuration only. The `_ensure_loaded()` guard
in `generate_abstract()` triggers model load on first invocation.

**Precondition:** Qwen3-30B-A3B-Instruct-2507 model weights available locally.

**Input:**
1. Construct provider with valid model ID.
2. Verify `_model` is None (not loaded).
3. Call `generate_abstract()` with sample text.
4. Verify `_model` is not None (loaded).

**Expected:**
- After construction: `provider._model is None`
- After first call: `provider._model is not None`
- The call returns a valid non-empty abstract

**Rationale:** Confirms the lazy loading contract: construction is cheap, load
happens on demand. Tests the internal state transition from unloaded to loaded.

### TEST-SAGE-AD-096: Second call reuses already-loaded model

**Artifact:** `sage/adapters/abstraction_qwen3.py` (Qwen3AbstractionProvider)
**Category:** initialization, performance
**Decision:** `_ensure_loaded()` is idempotent. After the first load, subsequent
calls skip the load path entirely.

**Precondition:** Provider constructed, first `generate_abstract()` already called.

**Input:** Call `generate_abstract()` a second time with different text.

**Expected:**
- Second call succeeds and returns a valid abstract
- The model object identity is the same as after the first call (no reload)

**Rationale:** Prevents accidental double-loading, which would waste ~16-20 GB
and add significant latency. The idempotency check is a simple `if self._model
is not None: return` guard.

### TEST-SAGE-AD-097: Model load failure on first call raises RuntimeError

**Artifact:** `sage/adapters/abstraction_qwen3.py` (Qwen3AbstractionProvider)
**Category:** initialization, error
**Decision:** If the model fails to load on first `generate_abstract()`, the
provider raises RuntimeError with a diagnostic message. The provider remains
in the unloaded state so that a retry (after fixing the environment) can
attempt loading again.

**Precondition:** Provider constructed with an invalid model ID.

**Input:** Call `generate_abstract()` with sample text.

**Expected:**
- Raises RuntimeError
- Error message includes the model ID
- `provider._model` remains None (load failure does not leave partial state)

**Rationale:** Preserves the fail-fast diagnostic contract from the previous
eager-loading design (AD-026 original). The error surfaces on first use
rather than at startup, but is equally clear.

---

## 10. LanceDB ContentStore -- has_chunks Existence Check (RETIRED)

Covered the `has_chunks(document_id)` existence probe against the
embedded LanceDB content store, retired along with the embedded storage
backend (CAS-ADR-042). The same contract is covered against the
Postgres content store by `test_has_chunks` in
`tests/sage/test_content_store_postgres.py`.
