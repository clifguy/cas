# SAGE Adapter Tests

Tier 2 behavioral tests for production adapter implementations: LanceDB
ContentStore and nomic-embed-text EmbeddingProvider.

These tests validate that the concrete adapter implementations satisfy the
abstract interfaces defined in `sage/adapters/interfaces.py` with real storage
and real embeddings. Distinct from the SAGE behavioral tests (TEST-SAGE-BH-*),
which validate service-layer logic against those same interfaces using stubs.

Each test encodes a design decision made during adapter specification (2026-04-06).
Tests are grouped by adapter in implementation dependency order: embedding
provider first (produces vectors consumed by content store tests), then content
store.

Test environment: tests use the test vault brain at `~/sage_vaults/test/brain/`
for LanceDB storage, referencing `~/sage_vaults/test/vault_config.yaml`.

---

## 1. nomic-embed-text EmbeddingProvider

### TEST-SAGE-AD-001: Embedding dimension is 768

**Artifact:** `sage/adapters/interfaces.py` (EmbeddingProvider)
**Category:** shape
**Decision:** nomic-embed-text produces 768-dimensional vectors. The provider validates this at init by embedding a probe text.

**Precondition:** nomic-embed-text model available via sentence-transformers.

**Input:** `embed(["The patent claims a novel method for data synchronization."])`

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

**Input:** `embed(["Short text.", "A significantly longer passage with multiple sentences about various topics including patent law, data management, and retrieval systems."])`

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

## 2. LanceDB ContentStore

### TEST-SAGE-AD-009: Lazy table creation on first index_chunks

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.index_chunks)
**Category:** initialization
**Decision:** The `chunks` table is created on the first `index_chunks` call. No explicit initialization step required.

**Precondition:** Empty brain_root directory (no prior LanceDB data).

**Input:** Call `index_chunks("doc_001", [chunk_with_embedding])`.

**Expected:**
- No error on first call
- A `chunks` table now exists in the LanceDB database at brain_root
- The table has columns: document_id (string), heading_path (string), content (string), chunk_index (int), vector (768-dim float)

**Rationale:** Lazy creation avoids requiring a separate init step in the vault
startup sequence. The content store is usable as soon as the first document is
ingested.

### TEST-SAGE-AD-010: Search on empty store returns empty results

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.search_semantic, search_bm25)
**Category:** initialization
**Decision:** If no `chunks` table exists (no documents indexed yet), search methods return empty lists rather than raising errors.

**Precondition:** Empty brain_root directory (no prior LanceDB data).

**Input:**
- `search_semantic(query_embedding=[0.1]*768, limit=10)`
- `search_bm25(query="test", limit=10)`

**Expected:**
- Both return `[]`
- No exceptions raised

**Rationale:** A freshly initialized vault with no documents is a valid state.
The retrieval service should handle empty results gracefully without needing to
check whether the store has been populated.

### TEST-SAGE-AD-011: Index and retrieve chunks roundtrip

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.index_chunks, get_all_chunks)
**Category:** indexing
**Decision:** Chunks stored via index_chunks are faithfully retrievable via get_all_chunks with all fields preserved.

**Precondition:** Content store initialized (brain_root exists).

**Input:** Index 3 chunks for document "doc_001":
- chunk_index=0, heading_path="Introduction", content="First paragraph.", embedding=[known vector]
- chunk_index=1, heading_path="Introduction > Background", content="Second paragraph.", embedding=[known vector]
- chunk_index=2, heading_path="Methods", content="Third paragraph.", embedding=[known vector]

Then call `get_all_chunks("doc_001")`.

**Expected:**
- Returns exactly 3 Chunk objects
- Returned in document order (chunk_index 0, 1, 2)
- Each chunk's document_id, heading_path, content, and chunk_index match the input
- Embeddings are preserved (element-wise match within 1e-6 tolerance)

**Rationale:** Faithful roundtrip is the fundamental correctness property. The
export_projection utility reconstructs document text from stored chunks, so
content must be preserved exactly. Embedding preservation is required for the
content store to serve as the single vector storage layer.

### TEST-SAGE-AD-012: Chunks from multiple documents are isolated

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.get_all_chunks)
**Category:** indexing
**Decision:** get_all_chunks returns only chunks for the requested document_id.

**Precondition:** Content store with chunks indexed for "doc_001" and "doc_002".

**Input:** `get_all_chunks("doc_001")`

**Expected:**
- Returns only chunks where document_id == "doc_001"
- No chunks from "doc_002" are included

**Rationale:** Document isolation is essential for correct export_projection and
deterministic retrieval. Cross-contamination would produce incorrect document
reconstructions.

### TEST-SAGE-AD-013: get_all_chunks for non-existent document returns empty list

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.get_all_chunks)
**Category:** edge_case
**Decision:** Querying for a document_id with no indexed chunks returns an empty list, not an error.

**Precondition:** Content store initialized (may or may not have other documents).

**Input:** `get_all_chunks("nonexistent_doc_999")`

**Expected:**
- Returns `[]`
- No exception raised

**Rationale:** The service layer checks pipeline_status before calling get_all_chunks,
but defensive behavior in the adapter prevents cascading errors if the check is
ever bypassed or if a document's chunks were removed during force re-ingestion.

### TEST-SAGE-AD-014: remove_document clears all chunks for a document

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.remove_document)
**Category:** removal
**Decision:** remove_document deletes all chunks matching the document_id via filter delete. FTS index is rebuilt eagerly after deletion.

**Precondition:** Content store with chunks indexed for "doc_001" (3 chunks) and "doc_002" (2 chunks).

**Input:** `remove_document("doc_001")`

**Expected:**
- `get_all_chunks("doc_001")` returns `[]`
- `get_all_chunks("doc_002")` still returns 2 chunks (unaffected)
- Subsequent `search_bm25` does not return results from "doc_001"

**Rationale:** Force re-ingestion (TEST-SAGE-BH-019) calls remove_document before
re-indexing. Stale chunks must be fully cleared, and the FTS index must reflect
the deletion immediately to prevent stale keyword search results.

### TEST-SAGE-AD-015: remove_document is idempotent

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.remove_document)
**Category:** removal
**Decision:** Removing a non-existent document_id is a no-op, not an error.

**Precondition:** Content store with no chunks for "nonexistent_doc".

**Input:** `remove_document("nonexistent_doc")`

**Expected:**
- No exception raised
- Store state is unchanged

**Rationale:** Idempotent removal simplifies the ingestion pipeline's force
re-ingestion path: it can always call remove_document before indexing without
checking whether previous chunks exist.

### TEST-SAGE-AD-016: Semantic search returns results ranked by cosine similarity

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.search_semantic)
**Category:** vector_search
**Decision:** search_semantic uses cosine distance and returns results in descending similarity order.

**Precondition:** Content store with chunks from 3 documents. Chunks have real
embeddings from nomic-embed-text (not stubs). Documents cover distinct topics:
- doc_a: medical record synchronization
- doc_b: healthcare data management
- doc_c: basketball statistics

**Input:** `search_semantic(query_embedding=embed("health data sync")[0], limit=5)`

**Expected:**
- Results are SearchResult objects with document_id, heading_path, content, score
- Scores are in descending order (highest similarity first)
- doc_a and doc_b chunks rank above doc_c chunks
- `limit` is respected (at most 5 results)

**Rationale:** This is the end-to-end validation that LanceDB cosine search with
real nomic-embed-text vectors produces semantically meaningful rankings. The stub
tests validate the interface contract; this test validates the actual retrieval
quality with production components.

### TEST-SAGE-AD-017: Semantic search respects limit parameter

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.search_semantic)
**Category:** vector_search
**Decision:** The limit parameter caps the number of returned results.

**Precondition:** Content store with 10+ chunks across multiple documents.

**Input:** `search_semantic(query_embedding=some_vector, limit=3)`

**Expected:**
- Returns at most 3 SearchResult objects

**Rationale:** The retrieval service passes limit from the API request. The
content store must enforce this to prevent unbounded result sets.

### TEST-SAGE-AD-018: BM25 search matches on content keywords

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.search_bm25)
**Category:** keyword_search
**Decision:** search_bm25 uses LanceDB native FTS index on the content column. Results are ranked by relevance.

**Precondition:** Content store with chunks containing varied content:
- Chunk A: "The synchronization protocol ensures data consistency across nodes."
- Chunk B: "Basketball teams compete in regional tournaments each spring."
- Chunk C: "Data synchronization is critical for distributed health systems."

**Input:** `search_bm25(query="synchronization data", limit=10)`

**Expected:**
- Chunks A and C are returned (both contain "synchronization" and/or "data")
- Chunk B is either absent or ranked below A and C
- Results are SearchResult objects with scores in descending order

**Rationale:** BM25 keyword search is the second component of hybrid RRF retrieval
(TEST-SAGE-BH-027). The FTS index must correctly match query terms against chunk
content.

### TEST-SAGE-AD-019: BM25 search reflects mutations after FTS index rebuild

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.search_bm25, index_chunks, remove_document)
**Category:** keyword_search
**Decision:** FTS index is rebuilt eagerly after every mutation. New chunks are searchable immediately; removed chunks are no longer searchable.

**Precondition:** Content store with chunks indexed for "doc_001".

**Input:**
1. `search_bm25(query="unique_keyword_xyz", limit=10)` -- returns no results
2. Index a new chunk for "doc_002" containing "unique_keyword_xyz"
3. `search_bm25(query="unique_keyword_xyz", limit=10)` -- should find the new chunk
4. `remove_document("doc_002")`
5. `search_bm25(query="unique_keyword_xyz", limit=10)` -- should return no results again

**Expected:**
- Step 1: empty results
- Step 3: returns chunk from doc_002
- Step 5: empty results (FTS index reflects removal)

**Rationale:** Eager FTS rebuild ensures search results are always consistent with
the current store state. A stale FTS index would return phantom results for
removed documents or miss newly indexed content.

### TEST-SAGE-AD-020: Heading prefix retrieval -- exact match

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.get_chunks_by_heading_prefix)
**Category:** heading_retrieval
**Decision:** Chunks whose heading_path exactly equals the prefix are included.

**Precondition:** Content store with chunks for "doc_001":
- heading_path="Methods", chunk_index=0
- heading_path="Methods > Sampling", chunk_index=1
- heading_path="Results", chunk_index=2

**Input:** `get_chunks_by_heading_prefix("doc_001", "Methods")`

**Expected:**
- Returns 2 chunks: "Methods" (exact match) and "Methods > Sampling" (child match)
- Returned in document order (chunk_index 0, then 1)
- "Results" chunk is not included

**Rationale:** Deterministic retrieval mode (TEST-SAGE-BH-029) uses heading prefix
to select a section and all its subsections. The filter expression
`heading_path = prefix OR heading_path LIKE 'prefix > %'` captures both the
exact section and its children.

### TEST-SAGE-AD-021: Heading prefix retrieval -- no match returns empty list

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.get_chunks_by_heading_prefix)
**Category:** heading_retrieval
**Decision:** A prefix that matches no chunks returns an empty list, not an error.

**Precondition:** Content store with chunks for "doc_001" (none with heading "Nonexistent Section").

**Input:** `get_chunks_by_heading_prefix("doc_001", "Nonexistent Section")`

**Expected:**
- Returns `[]`

**Rationale:** The service layer returns a 404 when no headings match. The adapter
should return empty results and let the service layer handle the error semantics.

### TEST-SAGE-AD-022: Heading prefix does not match partial heading names

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.get_chunks_by_heading_prefix)
**Category:** heading_retrieval
**Decision:** Prefix matching is structural (exact heading or child via " > " separator), not substring.

**Precondition:** Content store with chunks:
- heading_path="Method"
- heading_path="Methods"
- heading_path="Methods > Sampling"
- heading_path="Methodology"

**Input:** `get_chunks_by_heading_prefix("doc_001", "Method")`

**Expected:**
- Returns only the chunk with heading_path="Method"
- Does NOT return "Methods", "Methods > Sampling", or "Methodology"

**Rationale:** Substring matching would incorrectly include unrelated sections.
The " > " separator in the LIKE pattern prevents "Method" from matching "Methods"
or "Methodology". Only exact heading or `heading > child` paths qualify.

### TEST-SAGE-AD-023: Persistence across close and reopen

**Artifact:** `sage/adapters/interfaces.py` (ContentStore)
**Category:** persistence
**Decision:** Data written to the content store persists on disk and survives a close/reopen cycle.

**Precondition:** Content store initialized at a brain_root path.

**Input:**
1. Index chunks for "doc_001"
2. Close the content store (release LanceDB connection)
3. Create a new content store instance pointing to the same brain_root
4. Call `get_all_chunks("doc_001")`

**Expected:**
- Step 4 returns the same chunks indexed in step 1
- All fields preserved (document_id, heading_path, content, chunk_index, embedding)

**Rationale:** LanceDB writes to disk automatically. This test verifies that no
in-memory-only state is lost across process restarts, which is critical for a
vault that persists between SAGE server invocations.

### TEST-SAGE-AD-024: Special characters in heading_path are handled correctly

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.get_chunks_by_heading_prefix)
**Category:** edge_case
**Decision:** Heading paths containing special characters (apostrophes, parentheses, quotes) are stored and retrieved correctly.

**Precondition:** Content store initialized.

**Input:** Index a chunk with heading_path `"Section 3.1 > Smith's Method (2024)"`.
Then call `get_chunks_by_heading_prefix("doc_001", "Section 3.1 > Smith's Method (2024)")`.

**Expected:**
- The chunk is returned correctly
- Special characters in the heading_path do not cause SQL injection or filter expression errors

**Rationale:** Real document headings contain apostrophes, parentheses, and other
punctuation. The adapter must properly escape or parameterize these in LanceDB
filter expressions to prevent query failures or incorrect matches.

### TEST-SAGE-AD-025: Index_chunks replaces existing chunks for same document

**Artifact:** `sage/adapters/interfaces.py` (ContentStore.index_chunks)
**Category:** indexing
**Decision:** If index_chunks is called for a document_id that already has chunks, the old chunks are replaced (not appended).

**Precondition:** Content store with 3 chunks indexed for "doc_001".

**Input:** Call `index_chunks("doc_001", [new_chunk_a, new_chunk_b])` with 2 new chunks.

**Expected:**
- `get_all_chunks("doc_001")` returns exactly 2 chunks (the new ones)
- The 3 old chunks are no longer present
- FTS index reflects the new content

**Rationale:** The ingestion pipeline calls remove_document then index_chunks
during force re-ingestion. However, index_chunks should also be safe to call
directly without a preceding remove, preventing duplicate chunks from
accumulating if the caller forgets the removal step.

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

### TEST-SAGE-AD-026: Provider init loads model eagerly and fails fast

**Artifact:** `sage/adapters/interfaces.py` (AbstractionProvider)
**Category:** initialization
**Decision:** The provider loads the MLX model during `__init__` and validates
that inference is functional. If the model cannot be loaded, init raises
immediately.

**Precondition:** Qwen3-30B-A3B-Instruct-2507 model weights available locally.

**Input:** Construct a Qwen3AbstractionProvider with valid model path.

**Expected:**
- Construction completes without error
- Model is loaded into memory (not deferred to first `generate_abstract` call)

**Negative input:** Construct with `model_path="/nonexistent/model/path"`.

**Negative expected:**
- Raises an exception during `__init__`
- Error message includes the model path

**Rationale:** Same fail-fast pattern as the embedding provider (AD-008).
Deferred loading would allow the vault to initialize successfully but fail on
the first ingestion, which is harder to diagnose. Eager loading surfaces
configuration errors at startup.

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
50. The PIM Health vault sets this to 500. The provider must honor this bound
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

**Rationale:** PIM patent documents can be lengthy. While the pipeline-level
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
