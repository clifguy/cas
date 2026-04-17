# SAGE Behavioral Tests

Tier 2 behavioral tests for the SAGE storage layer, lifecycle state machine,
ingestion pipeline, retrieval subsystem, graph operations, and utilities.

Each test encodes a design decision made during tier 2 specification (2026-04-05).
Tests are grouped by subsystem in implementation dependency order.

---

## 1. Graph Store Foundation

### TEST-SAGE-BH-001: Document ID format

**Artifact:** `sage/sage_core_api.openapi.yaml` (Document.id)
**Category:** identity
**Decision:** Doc ID = Hash(source_path + created_at) truncated to 8 hex + slugified title

**Precondition:** SAGE vault initialized.

**Input:** Ingest a document with source path `patents/2026-03-09_PIM_PV06_Claim_Set_v6_12.docx`.

**Expected:**
- `id` matches pattern `^[0-9a-f]{8}_[a-z0-9_]+$`
- Title fragment is lowercase, alphanumeric + underscores only
- Title fragment is derived from the document title (not the full filename)

**Rationale:** Human-readable IDs improve log inspection and debugging. The hash
component provides uniqueness; the slug provides quick visual identification.

### TEST-SAGE-BH-002: Document ID uniqueness -- different paths, same timestamp

**Artifact:** `sage/sage_core_api.openapi.yaml` (Document.id)
**Category:** identity
**Decision:** Hash inputs include source_path, so different paths produce different IDs.

**Precondition:** SAGE vault initialized.

**Input:** Ingest two documents at the same created_at timestamp from different paths:
- `patents/doc_a.docx`
- `patents/doc_b.docx`

**Expected:** The two documents have different `id` values.

**Rationale:** source_path is part of the hash input, ensuring uniqueness across
concurrent ingestions from different files.

### TEST-SAGE-BH-003: Document ID uniqueness -- same path, different timestamps

**Artifact:** `sage/sage_core_api.openapi.yaml` (Document.id)
**Category:** identity
**Decision:** Hash inputs include created_at, so the same path at different times produces different IDs.

**Precondition:** SAGE vault initialized.

**Input:** Ingest from `patents/doc_a.docx` at two different timestamps (simulating
re-ingestion of a changed file with `force: true`).

**Expected:** The two ingest operations produce different `id` values.

**Rationale:** created_at is part of the hash input. Note: `force: true` re-ingestion
reuses the existing ID (see TEST-SAGE-BH-019); this test covers the non-force case
where a genuinely new document record is created.

### TEST-SAGE-BH-004: SQLite WAL mode enabled at startup

**Artifact:** SAGE graph store initialization
**Category:** storage
**Decision:** WAL mode with per-document locking.

**Precondition:** Fresh vault initialization.

**Input:** Initialize vault, then query `PRAGMA journal_mode;` on the graph store.

**Expected:** Returns `wal`.

**Rationale:** WAL mode enables concurrent readers and is required for the
per-document locking strategy.

### TEST-SAGE-BH-005: Concurrent writes to different documents succeed

**Artifact:** SAGE graph store concurrency
**Category:** storage
**Decision:** Per-document locking allows concurrent writes to distinct documents.

**Precondition:** Two documents (doc_a, doc_b) ingested and active.

**Input:** Issue `update_metadata` on doc_a and doc_b concurrently (parallel async calls).

**Expected:** Both calls return 200. Neither blocks the other.

**Rationale:** Per-document locks allow maximum concurrency for independent operations.

### TEST-SAGE-BH-006: Concurrent writes to the same document serialize

**Artifact:** SAGE graph store concurrency
**Category:** storage
**Decision:** Per-document locking serializes writes to the same document.

**Precondition:** Document doc_a ingested and active.

**Input:** Issue two `set_lifecycle` calls on doc_a concurrently.

**Expected:** Both calls complete (one succeeds, the second either succeeds if the
transition is still valid after the first completes, or returns 409 if the first
transition changed the state). Neither call receives a raw SQLITE_BUSY error.

**Rationale:** Application-level per-document locking prevents database-level
contention from reaching callers.

### TEST-SAGE-BH-007: indexed_at is null before indexing completes

**Artifact:** `sage/sage_core_api.openapi.yaml` (Document.indexed_at)
**Category:** storage
**Decision:** indexed_at is nullable; null until indexing completes.

**Precondition:** SAGE vault initialized with abstraction enabled.

**Input:** Ingest a document. Immediately call `get_document` before indexing completes.

**Expected:**
- `pipeline_status` is `projection_complete` or `indexing_in_progress`
- `indexed_at` is `null`

**Rationale:** Nullable indexed_at is the cleanest semantic representation of
"not yet indexed." Avoids sentinel values.

### TEST-SAGE-BH-008: indexed_at populated after indexing completes

**Artifact:** `sage/sage_core_api.openapi.yaml` (Document.indexed_at)
**Category:** storage
**Decision:** indexed_at set when indexing completes, not updated by abstraction.

**Precondition:** Document ingested; wait for indexing to complete.

**Input:** Call `get_document` after pipeline reaches `indexing_complete`.

**Expected:**
- `indexed_at` is a valid ISO 8601 timestamp
- `indexed_at` does not change when pipeline advances to `abstraction_complete`

**Rationale:** indexed_at captures when content entered the content store,
independent of abstraction.


---

## 2. Access Control

### TEST-SAGE-BH-009: Vault owner auto-registered at initialization

**Artifact:** SAGE vault initialization, `vault_config.yaml` (vault.owner)
**Category:** access_control
**Decision:** Vault init reads owner from config and auto-creates user record.

**Precondition:** Fresh vault with `vault.owner: clif`.

**Input:** Initialize the vault. Query the user table.

**Expected:**
- User table contains one record with `display_name: "clif"`, `type: "human"`
- The user_id is usable as `created_by` on subsequent API calls

**Rationale:** The owner always exists from the moment the vault does. No separate
bootstrap step required.

### TEST-SAGE-BH-010: ROOT Harness register_agent creates SAGE user

**Artifact:** ROOT Harness `register_agent` -> SAGE `register_user`
**Category:** access_control, boundary
**Decision:** ROOT Harness is authoritative for agent registration; it calls
SAGE register_user internally.

**Precondition:** Vault initialized. ROOT Harness running.

**Input:** Call ROOT Harness `register_agent` with agent name "glossary_steward".

**Expected:**
- ROOT Harness returns an agent record containing a `sage_user_id` field
- SAGE user table contains a record with `type: "agent"` matching `sage_user_id`
- The SAGE user's `display_name` matches the agent's registered name

**Rationale:** Single entry point for agent registration. ROOT Harness calls into
SAGE (boundary rule direction), not the reverse.

### TEST-SAGE-BH-011: Direct SAGE register_user with type agent succeeds

**Artifact:** `sage/sage_core_api.openapi.yaml` (register_user)
**Category:** access_control
**Decision:** SAGE's API allows agent registration directly (ROOT Harness is the
intended caller, not the only possible one).

**Precondition:** Vault initialized.

**Input:** Call SAGE `register_user` with `type: "agent"`, `display_name: "test_agent"`.

**Expected:** 201, user record returned with `type: "agent"`.

**Rationale:** SAGE is not coupled to ROOT Harness. The register_user endpoint
accepts any valid user type.


---

## 3. Lifecycle State Machine

### TEST-SAGE-BH-012: Invalid transition returns 409 with valid_actions

**Artifact:** `sage/sage_core_api.openapi.yaml` (set_lifecycle, 409 response)
**Category:** lifecycle, error_semantics
**Decision:** 409 detail includes current_state, attempted_action, and valid_actions.

**Precondition:** Document in `archived` state.

**Input:** `set_lifecycle(action: "complete")`

**Expected:**
- HTTP 409
- `code: "invalid_lifecycle_transition"`
- `detail.current_state: "archived"`
- `detail.attempted_action: "complete"`
- `detail.valid_actions: ["reactivate"]`

**Rationale:** Discoverability: the error response tells callers what they can do
from the current state without requiring a separate lookup.

### TEST-SAGE-BH-013: valid_actions reflects domain-specific transitions

**Artifact:** `sage/sage_core_api.openapi.yaml` (set_lifecycle, 409 response)
**Category:** lifecycle, error_semantics
**Decision:** valid_actions is derived from the vault's full transition table,
including domain-specific transitions.

**Precondition:** PIM Health vault. Document in `active` state.

**Input:** `set_lifecycle(action: "nonexistent_action")`

**Expected:**
- HTTP 409 (or 400 for unknown action -- see note below)
- `detail.valid_actions` includes `["supersede", "complete", "archive", "file"]`
- The `file` action is PIM Health domain-specific

**Note:** If the action value itself is unknown (not in any transition table),
400 (invalid action value) is returned per the existing OpenAPI spec. If the action
is known but invalid from the current state, 409 is returned. This test should
use a known action that is invalid from the current state (e.g., `reactivate` on
an `active` document).

**Revised input:** `set_lifecycle(action: "reactivate")` on an `active` document.

**Expected:**
- HTTP 409
- `detail.valid_actions: ["supersede", "complete", "archive", "file"]`

**Rationale:** Domain-aware error responses require SAGE to consult the vault's
lifecycle configuration, not just the base transition table.

### TEST-SAGE-BH-014: Lifecycle transition allowed during pipeline, with warning

**Artifact:** `sage/sage_core_api.openapi.yaml` (set_lifecycle, 200 response)
**Category:** lifecycle, pipeline_coordination
**Decision:** Transitions succeed regardless of pipeline status; response includes
warnings when pipeline is non-terminal.

**Precondition:** Document with `pipeline_status: indexing_in_progress`.

**Input:** `set_lifecycle(action: "supersede", new_version_id: <valid_id>)`

**Expected:**
- HTTP 200
- Document `lifecycle_status` is `archived`
- Document `pipeline_status` unchanged (`indexing_in_progress`)
- Response includes `warnings` array containing a message about the in-progress pipeline

**Rationale:** Lifecycle and pipeline are independent state dimensions. Blocking
transitions on pipeline status would prevent legitimate corrections (e.g.,
superseding a document that was ingested incorrectly).

### TEST-SAGE-BH-015: Lifecycle transition with no pipeline warning when terminal

**Artifact:** `sage/sage_core_api.openapi.yaml` (set_lifecycle, 200 response)
**Category:** lifecycle, pipeline_coordination
**Decision:** No warning when pipeline_status is terminal.

**Precondition:** Document with `pipeline_status: abstraction_complete`.

**Input:** `set_lifecycle(action: "archive")`

**Expected:**
- HTTP 200
- `warnings` is absent or empty array

**Rationale:** Warnings should only appear when there is genuinely something to warn about.

### TEST-SAGE-BH-016: Supersede requires existing new_version_id

**Artifact:** `sage/sage_core_api.openapi.yaml` (set_lifecycle, supersede action)
**Category:** lifecycle, referential_integrity
**Decision:** Strict referential integrity: new_version_id must resolve to an existing document.

**Precondition:** Document doc_old in `active` state.

**Input:** `set_lifecycle(action: "supersede", new_version_id: "nonexistent_id")`

**Expected:**
- HTTP 404
- `code: "document_not_found"`
- `detail.document_id: "nonexistent_id"`

**Rationale:** Enforces ingest-then-supersede ordering. Prevents dangling supersedes
edges in the graph.

### TEST-SAGE-BH-017: Supersede creates supersedes edge

**Artifact:** `sage/sage_core_api.openapi.yaml` (set_lifecycle, supersede action)
**Category:** lifecycle, graph_side_effects
**Decision:** Supersede creates an edge from new_version to old document.

**Precondition:** doc_old (active), doc_new (active, already ingested).

**Input:** `set_lifecycle` on doc_old with `action: "supersede", new_version_id: doc_new.id`

**Expected:**
- doc_old transitions to `archived`
- A `supersedes` edge exists with `source_id: doc_new.id`, `target_id: doc_old.id`
- The edge has an auto-generated `id` field

**Rationale:** The supersedes edge direction (new -> old) allows traversal from
any document to its predecessors via outbound edges.


---

## 4. Ingestion Pipeline

### TEST-SAGE-BH-018: Duplicate content detection returns 409

**Artifact:** `sage/sage_core_api.openapi.yaml` (ingest endpoint)
**Category:** ingestion, duplicate_detection
**Decision:** Same source_path + same source_content_hash = 409 Conflict.

**Precondition:** Document A already ingested from `patents/doc_a.docx` with hash H.

**Input:** Ingest from `patents/doc_a.docx` with identical content (hash H), no force flag.

**Expected:**
- HTTP 409
- `code: "duplicate_content"`
- `detail.existing_document_id: <doc_A_id>`
- `detail.source_content_hash: H`

**Rationale:** Prevents accidental duplicate ingestion. Callers must explicitly
decide to re-ingest via the force flag.

### TEST-SAGE-BH-019: Force re-ingestion bypasses duplicate detection

**Artifact:** `sage/sage_core_api.openapi.yaml` (ingest endpoint, force flag)
**Category:** ingestion, duplicate_detection
**Decision:** force: true re-runs the full pipeline on the existing document record.

**Precondition:** Document A already ingested from `patents/doc_a.docx` with hash H.

**Input:** Ingest from `patents/doc_a.docx` with identical content, `force: true`.

**Expected:**
- HTTP 200 (not 201 -- existing record, re-processed)
- Document `id` is unchanged (same as doc_A)
- `pipeline_status` resets to `projection_complete`
- `projected_at` is updated to current time
- `adapter_version` reflects the current adapter version

**Rationale:** Provides a clean recovery path after pipeline failures without
creating duplicate document records.

### TEST-SAGE-BH-020: Failed pipeline quarantines document from retrieval

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, pipeline interaction)
**Category:** ingestion, retrieval_interaction
**Decision:** pipeline_status: failed = fully unusable until re-ingested.

**Precondition:** Document A with `pipeline_status: failed`.

**Input:** `discover(mode: "semantic", query: "test query")` where doc_A would
normally match.

**Expected:** doc_A does not appear in results.

**Rationale:** Strict quality gate. Documents with incomplete pipelines should
not pollute search results.

### TEST-SAGE-BH-021: Failed document excluded from deterministic retrieval

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, deterministic mode)
**Category:** ingestion, retrieval_interaction
**Decision:** All retrieval modes reject failed documents.

**Precondition:** Document A with `pipeline_status: failed`.

**Input:** `discover(mode: "deterministic", document_id: doc_A.id, heading_path: "Section 1")`

**Expected:**
- HTTP 422
- `code: "pipeline_incomplete"`

**Rationale:** Even deterministic extraction requires a valid projection. A failed
pipeline means the projection may be corrupt or incomplete.

### TEST-SAGE-BH-022: Failed document still visible via get_document

**Artifact:** `sage/sage_core_api.openapi.yaml` (get_document)
**Category:** ingestion
**Decision:** get_document returns the record regardless of pipeline status.

**Precondition:** Document A with `pipeline_status: failed`.

**Input:** `get_document(document_id: doc_A.id)`

**Expected:**
- HTTP 200
- `pipeline_status: "failed"`
- `pipeline_error` contains failure description

**Rationale:** Callers need to see the failure to diagnose and decide on recovery.

### TEST-SAGE-BH-023: Failed document does not satisfy preconditions

**Artifact:** `sage/sage_core_api.openapi.yaml` (check_preconditions)
**Category:** ingestion, graph_interaction
**Decision:** Failed documents do not satisfy depends_on checks.

**Precondition:** doc_function depends_on doc_dependency. doc_dependency has
`pipeline_status: failed`.

**Input:** `check_preconditions(function_id: doc_function.id)`

**Expected:**
- `satisfied: false`
- Check for doc_dependency: `satisfied: false`, `actual: "failed (pipeline_incomplete)"`

**Rationale:** A quarantined document is not a reliable dependency.

### TEST-SAGE-BH-024: LLM failure during abstraction results in failed status

**Artifact:** SAGE ingestion pipeline, Stage 3
**Category:** ingestion, llm_interaction
**Decision:** LLM failure = pipeline_status: failed. Strict quality gate.

**Precondition:** Vault with `abstraction.enabled: true`. LLM endpoint unavailable
(simulated via mock or network block).

**Input:** Ingest a document.

**Expected:**
- Stage 1 (projection) and Stage 2 (indexing) complete normally
- Stage 3 (abstraction) fails due to LLM unavailability
- `pipeline_status: "failed"`
- `pipeline_error` describes the LLM failure
- `semantic_abstract: null`

**Rationale:** CAS-ADR-011 specifies graceful degradation, but the design decision
is that degradation is a configuration choice (abstraction.enabled: false), not a
runtime fallback. LLM failure is a genuine failure requiring re-ingestion.

### TEST-SAGE-BH-025: Abstraction disabled produces abstraction_skipped

**Artifact:** SAGE ingestion pipeline, Stage 3
**Category:** ingestion, configuration
**Decision:** abstraction_skipped is reserved for deliberate configuration, not runtime failure.

**Precondition:** Vault with `abstraction.enabled: false`.

**Input:** Ingest a document.

**Expected:**
- Stage 1 and Stage 2 complete normally
- `pipeline_status: "abstraction_skipped"`
- `semantic_abstract: null`
- Document is fully usable in all retrieval modes

**Rationale:** Distinguishes between "we chose not to generate abstracts" and
"we tried and failed."

### TEST-SAGE-BH-026: Pipeline stages run sequentially within ingest

**Artifact:** SAGE ingestion pipeline, sequential execution model
**Category:** ingestion, execution
**Decision:** Stages 1-3 run sequentially within the `ingest()` call. The method
returns only after all stages complete (or fail). This replaces the previous
`asyncio.create_task` background model to cap peak memory during bulk ingest.

**Precondition:** SAGE vault initialized.

**Input:** Ingest a document and verify the returned result immediately.

**Expected:**
- Ingest returns with `pipeline_status: "abstraction_complete"`
- `indexed_at` is set (Stage 2 completed)
- `semantic_abstract` is set (Stage 3 completed)
- No background tasks are pending after the call returns

**Rationale:** Sequential execution ensures only one document's projection,
embeddings, and abstraction context reside in memory at a time, preventing
unbounded memory growth during bulk ingest. Throughput impact is acceptable
because Stage 3 (MLX inference) is the bottleneck and is inherently serial.


---

## 5. Retrieval

### TEST-SAGE-BH-027: Hybrid retrieval uses Reciprocal Rank Fusion

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, semantic mode)
**Category:** retrieval, fusion
**Decision:** Hybrid mode uses RRF: score = sum(1 / (k + rank)).

**Precondition:** Vault with multiple indexed documents. One document matches
well on vector similarity, another matches well on BM25 keyword match, a third
matches well on both.

**Input:** `discover(mode: "semantic", query: "test query", use_hybrid: true)`

**Expected:**
- Document matching both vector and BM25 ranks highest
- `relevance_score` reflects the RRF-computed score
- Results are ordered by RRF score descending

**Rationale:** RRF is rank-based, doesn't require score normalization across
heterogeneous retrieval systems.

### TEST-SAGE-BH-028: Non-hybrid retrieval uses pure vector scores

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, semantic mode)
**Category:** retrieval
**Decision:** use_hybrid: false returns pure vector similarity scores (override of
the default hybrid behavior).

**Precondition:** Vault with indexed documents.

**Input:** `discover(mode: "semantic", query: "test query", use_hybrid: false)`

**Expected:**
- `relevance_score` reflects vector distance/similarity
- No BM25 influence on ranking

**Rationale:** Default behavior is hybrid RRF; pure vector search is opt-in for
cases where only semantic similarity is desired.

### TEST-SAGE-BH-029: Deterministic retrieval uses prefix match on heading_path

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, deterministic mode)
**Category:** retrieval, deterministic
**Decision:** heading_path matches the specified heading and all children.

**Precondition:** Document with heading hierarchy:
```
Section 3 > Definitions
Section 3 > Definitions > Normalization
Section 3 > Definitions > Normalization > Overview
Section 3 > Definitions > Normalization > Rules
```

**Input:** `discover(mode: "deterministic", document_id: doc.id, heading_path: "Section 3 > Definitions > Normalization")`

**Expected:**
- Results include chunks from:
  - `Section 3 > Definitions > Normalization`
  - `Section 3 > Definitions > Normalization > Overview`
  - `Section 3 > Definitions > Normalization > Rules`
- Chunks are returned in document order (heading hierarchy order)
- `relevance_score` is null for all results

**Rationale:** Prefix match is the natural behavior for governed content transfer.
"Give me the Normalization section" means the section and everything under it.

### TEST-SAGE-BH-030: Deterministic retrieval with non-existent heading returns 404

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, deterministic mode)
**Category:** retrieval, error_semantics
**Decision:** Missing heading path returns 404.

**Precondition:** Document exists with known headings.

**Input:** `discover(mode: "deterministic", document_id: doc.id, heading_path: "Nonexistent > Section")`

**Expected:**
- HTTP 404
- `code: "heading_not_found"`

**Rationale:** Clear error for heading path resolution failures.


---

## 6. Graph Operations

### TEST-SAGE-BH-031: Duplicate edges are permitted

**Artifact:** `sage/sage_core_api.openapi.yaml` (link endpoint)
**Category:** graph
**Decision:** Multiple edges of the same type between the same documents are allowed.

**Precondition:** doc_a and doc_b both exist and active.

**Input:** Call `link(source_id: doc_a, target_id: doc_b, edge_type: "references",
rationale: "First rationale")` then call `link(source_id: doc_a, target_id: doc_b,
edge_type: "references", rationale: "Updated understanding")`.

**Expected:**
- Both calls return 201
- Each returned edge has a distinct `id` field
- Both edges exist in the graph store

**Rationale:** Models evolving understanding. Each edge captures a distinct decision
with its own rationale and timestamp.

### TEST-SAGE-BH-032: Edge records have auto-generated IDs

**Artifact:** `sage/sage_core_api.openapi.yaml` (Edge schema)
**Category:** graph
**Decision:** Edges have an auto-generated id field for unambiguous identity.

**Precondition:** Two documents exist.

**Input:** `link(source_id: doc_a, target_id: doc_b, edge_type: "references")`

**Expected:**
- Response includes `id` field on the Edge object
- `id` is a non-empty string

**Rationale:** Required for disambiguation when duplicate edges exist, and for
future edge-level operations (update, annotate).

### TEST-SAGE-BH-033: check_preconditions -- active satisfies dependency

**Artifact:** `sage/sage_core_api.openapi.yaml` (check_preconditions)
**Category:** graph, lifecycle_interaction
**Decision:** Only active and completed satisfy depends_on checks.

**Precondition:** doc_function depends_on doc_dep. doc_dep is `active`.

**Input:** `check_preconditions(function_id: doc_function.id)`

**Expected:** `satisfied: true`, individual check: `satisfied: true`

**Rationale:** Active documents are current and available.

### TEST-SAGE-BH-034: check_preconditions -- completed satisfies dependency

**Artifact:** `sage/sage_core_api.openapi.yaml` (check_preconditions)
**Category:** graph, lifecycle_interaction
**Decision:** Completed is a valid dependency state.

**Precondition:** doc_function depends_on doc_dep. doc_dep is `completed`.

**Input:** `check_preconditions(function_id: doc_function.id)`

**Expected:** `satisfied: true`

**Rationale:** Completed documents are finished work, fully available.

### TEST-SAGE-BH-035: check_preconditions -- archived does not satisfy

**Artifact:** `sage/sage_core_api.openapi.yaml` (check_preconditions)
**Category:** graph, lifecycle_interaction
**Decision:** Archived documents are not in active use; they don't satisfy dependencies.

**Precondition:** doc_function depends_on doc_dep. doc_dep is `archived`.

**Input:** `check_preconditions(function_id: doc_function.id)`

**Expected:**
- `satisfied: false`
- Check `actual` field reports `archived`

**Rationale:** An archived document is no longer in active use (it may have been
superseded or simply retired). Callers should depend on the active replacement.

### TEST-SAGE-BH-036: check_preconditions -- filed does not satisfy (domain-specific)

**Artifact:** `sage/sage_core_api.openapi.yaml` (check_preconditions)
**Category:** graph, lifecycle_interaction
**Decision:** Only base states (active, completed) satisfy dependencies. Domain-specific
states are not known to SAGE's precondition checker.

**Precondition:** PIM Health vault. doc_function depends_on doc_patent. doc_patent
is `filed`.

**Input:** `check_preconditions(function_id: doc_function.id)`

**Expected:**
- `satisfied: false`
- Check `actual` field reports `filed`

**Rationale:** SAGE stays domain-agnostic. Orchestrators bring domain knowledge.
An orchestrator can read the `actual` field, see "filed", and decide in its own
logic that this is acceptable.

### TEST-SAGE-BH-037: Traversal deduplicates by document with edge_counts map

**Artifact:** `sage/sage_core_api.openapi.yaml` (traverse endpoint)
**Category:** graph, traversal
**Decision:** Deduplication by target document; most recent edge shown; `edge_counts`
map (keyed by edge type) reports per-type totals. Replaces scalar `edge_count`.

**Precondition:** doc_a has 3 `references` edges to doc_b (created at t1, t2, t3).

**Input:** `traverse(start_id: doc_a.id, edge_type: "references", direction: "outbound")`

**Expected:**
- doc_b appears once in results
- The edge shown is the one created at t3 (most recent)
- `edge_counts: {"references": 3}`

**Rationale:** Compact results with the signal that duplicates exist. Per-type
breakdown gives callers actionable information about relationship density without
a second query. Prevents result explosion in dense graphs.


---

## 7. Utilities

### TEST-SAGE-BH-038: export_projection enforces path containment

**Artifact:** `sage/sage_core_api.openapi.yaml` (export_projection)
**Category:** utilities, security
**Decision:** output_path must resolve within the vault's storage_root.

**Precondition:** Vault with `storage_root: "/tmp/test_vault/sources"`. Document ingested.

**Input:** `export_projection(output_path: "../../etc/passwd")`

**Expected:**
- HTTP 400
- `code: "path_traversal_denied"`

**Rationale:** Standard path traversal protection. Prevents accidental writes
to arbitrary filesystem locations.

### TEST-SAGE-BH-039: export_projection allows valid relative paths

**Artifact:** `sage/sage_core_api.openapi.yaml` (export_projection)
**Category:** utilities, security
**Decision:** Relative paths within storage_root are permitted.

**Precondition:** Vault with `storage_root: "/tmp/test_vault/sources"`. Document ingested.

**Input:** `export_projection(output_path: "exports/doc_a.md")`

**Expected:**
- HTTP 200
- File written at `/tmp/test_vault/sources/exports/doc_a.md`

**Rationale:** Normal export workflow uses relative paths within the vault.

### TEST-SAGE-BH-040: export_projection rejects absolute paths outside vault

**Artifact:** `sage/sage_core_api.openapi.yaml` (export_projection)
**Category:** utilities, security
**Decision:** Absolute paths that don't start with storage_root are rejected.

**Precondition:** Vault with `storage_root: "/tmp/test_vault/sources"`.

**Input:** `export_projection(output_path: "/home/user/outside.md")`

**Expected:**
- HTTP 400
- `code: "path_traversal_denied"`

**Rationale:** Absolute paths bypassing the vault boundary are rejected
alongside relative traversal attacks.

### TEST-SAGE-BH-041: Retrieval assertions loaded from separate YAML file

**Artifact:** `sage/sage_core_api.openapi.yaml` (eval_retrieval)
**Category:** utilities, configuration
**Decision:** Retrieval health assertions are defined in a per-vault YAML file,
referenced from vault config.

**Precondition:** Vault config includes `retrieval_health.assertions_file: "retrieval_assertions.yaml"`.
The file exists with valid assertions.

**Input:** `eval_retrieval()`

**Expected:**
- Assertions are loaded from the referenced file
- Results contain pass/fail per assertion
- Overall `passed` reflects whether all assertions passed

**Rationale:** Assertions as a separate file keeps vault config concise and
allows assertion maintenance independent of vault configuration.

### TEST-SAGE-BH-042: Missing assertions file returns error

**Artifact:** `sage/sage_core_api.openapi.yaml` (eval_retrieval)
**Category:** utilities, error_semantics
**Decision:** Missing or malformed assertions file produces a clear error.

**Precondition:** Vault config references a nonexistent assertions file.

**Input:** `eval_retrieval()`

**Expected:**
- HTTP 400
- `code: "assertions_file_not_found"` (or `"assertions_file_invalid"` for malformed)

**Rationale:** Clear error for operational misconfiguration.


### TEST-SAGE-BH-043: refresh_views generates by_doc_type and by_lifecycle directories

**Artifact:** `sage/sage_core_api.openapi.yaml` (refresh_views)
**Category:** utilities, filesystem
**Decision:** Both view dimensions (doc_type and lifecycle_status) are always
generated. Output location is `{storage_root}/views/by_doc_type/` and
`{storage_root}/views/by_lifecycle/`.

**Precondition:** Vault with three documents:
- doc_a: `doc_type: "patent"`, `lifecycle_status: "active"`
- doc_b: `doc_type: "patent"`, `lifecycle_status: "archived"`
- doc_c: `doc_type: "glossary"`, `lifecycle_status: "active"`

**Input:** `refresh_views()`

**Expected:**
- HTTP 200
- `views_generated` >= 3 (at least: `by_doc_type/patent`, `by_doc_type/glossary`,
  `by_lifecycle/active`)
- Directory `{storage_root}/views/by_doc_type/patent/` exists with symlinks to
  doc_a and doc_b source files
- Directory `{storage_root}/views/by_doc_type/glossary/` exists with symlink to
  doc_c source file
- Directory `{storage_root}/views/by_lifecycle/active/` exists with symlinks to
  doc_a and doc_c source files
- Directory `{storage_root}/views/by_lifecycle/archived/` exists with symlink to
  doc_b source file

**Rationale:** Both dimensions are cheap to generate and useful for different
browsing patterns. No configuration needed for what is always a small number
of directories.

### TEST-SAGE-BH-044: Symlinks point to original source files

**Artifact:** `sage/sage_core_api.openapi.yaml` (refresh_views)
**Category:** utilities, filesystem
**Decision:** Symlinks target the original source files at
`{storage_root}/{source_path}`.

**Precondition:** Document ingested from `patents/claim_set.docx`.

**Input:** `refresh_views()`

**Expected:**
- Symlink in `views/by_doc_type/{doc_type}/` resolves to
  `{storage_root}/patents/claim_set.docx`
- Symlink target is a relative path (not absolute) to remain valid if the
  vault root is moved
- Symlink name is the original filename (`claim_set.docx`)

**Rationale:** Humans browsing the filesystem want to open the original file,
not the structured projection. Relative symlink targets survive vault relocation.

### TEST-SAGE-BH-045: refresh_views performs full regeneration

**Artifact:** `sage/sage_core_api.openapi.yaml` (refresh_views)
**Category:** utilities, filesystem
**Decision:** Full regeneration: the `views/` directory is wiped and rebuilt
from current graph state on each call.

**Precondition:** Document doc_a initially `active`. Run `refresh_views()`.
Then `set_lifecycle(action: "archive")` on doc_a.

**Input:** `refresh_views()` (second call, after lifecycle change)

**Expected:**
- `views/by_lifecycle/active/` no longer contains a symlink to doc_a
- `views/by_lifecycle/archived/` contains a symlink to doc_a
- No stale symlinks remain from the previous generation

**Rationale:** Full regeneration is the simplest correct approach. The endpoint
description says "regenerate," not "update." The number of documents in a
personal vault makes incremental updates unnecessary.

### TEST-SAGE-BH-046: Failed-pipeline documents appear in views

**Artifact:** `sage/sage_core_api.openapi.yaml` (refresh_views)
**Category:** utilities, filesystem, pipeline_interaction
**Decision:** Views reflect graph state, not retrieval eligibility. Documents
with failed pipelines appear in views like any other document.

**Precondition:** Document doc_a with `pipeline_status: failed`,
`lifecycle_status: active`, `doc_type: "patent"`.

**Input:** `refresh_views()`

**Expected:**
- Symlink to doc_a appears in `views/by_lifecycle/active/`
- Symlink to doc_a appears in `views/by_doc_type/patent/`

**Rationale:** The filesystem view is for human browsing, not retrieval. A human
might want to find a failed document precisely to diagnose the failure. Hiding
quarantined documents from the filesystem would be inconsistent with
`get_document` returning them (BH-022).

### TEST-SAGE-BH-047: Empty doc_type or lifecycle status produces no directory

**Artifact:** `sage/sage_core_api.openapi.yaml` (refresh_views)
**Category:** utilities, filesystem
**Decision:** Only create subdirectories that contain at least one symlink.
`views_generated` counts the number of subdirectories created.

**Precondition:** Vault with one document: `doc_type: "patent"`,
`lifecycle_status: "active"`.

**Input:** `refresh_views()`

**Expected:**
- `views/by_doc_type/patent/` exists (one symlink)
- `views/by_lifecycle/active/` exists (one symlink)
- No other subdirectories under `by_doc_type/` or `by_lifecycle/`
- `views_generated: 2`

**Rationale:** Empty directories are clutter. The view count gives the caller
a quick signal of vault shape without listing directory contents.

### TEST-SAGE-BH-048: Documents with null doc_type excluded from by_doc_type view

**Artifact:** `sage/sage_core_api.openapi.yaml` (refresh_views)
**Category:** utilities, filesystem
**Decision:** Documents without a doc_type assignment are not represented in
the `by_doc_type/` view. They still appear in the `by_lifecycle/` view.

**Precondition:** Document doc_a with `doc_type: null`, `lifecycle_status: "active"`.

**Input:** `refresh_views()`

**Expected:**
- `views/by_doc_type/` has no subdirectory containing a symlink to doc_a
- `views/by_lifecycle/active/` contains a symlink to doc_a

**Rationale:** A null doc_type means the document has not been classified.
Creating a `by_doc_type/null/` or `by_doc_type/unclassified/` directory
would be misleading. The lifecycle view always has a value (documents enter
the vault as `active`), so it is always complete.

---

## 9. Source File Provenance

### TEST-SAGE-BH-049: New document ingestion sets source_modified_at from file mtime

**Artifact:** `sage/sage_core_api.openapi.yaml` (Document.source_modified_at)
**Category:** ingestion, provenance
**Decision:** The markdown adapter extracts `st_mtime` from the source file
and passes it through `ProjectionResult.metadata`. The ingestion service
parses it and stores it as `source_modified_at` on the Document.

**Precondition:** SAGE vault initialized. Source file exists with a known
modification time (set via `os.utime` for determinism).

**Input:** `ingest(source="test.md", adapter="markdown")`

**Expected:**
- `doc.source_modified_at` is not None
- `doc.source_modified_at` is a timezone-aware datetime (UTC)
- `doc.source_modified_at` matches the file's `st_mtime` (within 1-second tolerance)

**Rationale:** The source file's modification timestamp is a vital provenance
signal for graph ordering and retrieval relevance. Ingestion time (`created_at`)
is an operational detail; `source_modified_at` reflects when the content was
last changed at its origin.

### TEST-SAGE-BH-050: Force re-ingestion updates source_modified_at

**Artifact:** `sage/sage_core_api.openapi.yaml` (Document.source_modified_at)
**Category:** ingestion, provenance
**Decision:** On force re-ingestion, `source_modified_at` is updated to the
file's current `st_mtime`, even if the content hash is unchanged.

**Precondition:** Document already ingested. Source file touched (mtime updated)
but content unchanged.

**Input:** `ingest(source="test.md", adapter="markdown", force=True)`

**Expected:**
- `doc.source_modified_at` reflects the file's new mtime
- `doc.source_modified_at` differs from the original ingestion's value
- `doc.created_at` is unchanged (still the original SAGE ingestion time)

**Rationale:** Force re-ingestion is a recovery mechanism. The file's mtime
may have changed (e.g., after a restore or copy), and the document record
should reflect the current state of the source.

### TEST-SAGE-BH-051: source_modified_at round-trips through graph store

**Artifact:** `sage/storage/graph_store.py` (insert_document, get_document)
**Category:** graph_store, serialization
**Decision:** `source_modified_at` is serialized as ISO 8601 text in SQLite
and deserialized back to a datetime on retrieval, following the same pattern
as `projected_at` and `indexed_at`.

**Precondition:** SAGE vault initialized.

**Input:** Insert a Document with `source_modified_at` set to a known datetime.
Retrieve it via `get_document`.

**Expected:**
- Retrieved `doc.source_modified_at` equals the original value
- The value is a timezone-aware datetime

**Rationale:** Nullable datetime fields must survive the SQLite TEXT round-trip.
This test guards the serialization and deserialization paths.

### TEST-SAGE-BH-052: created_at remains SAGE ingestion time, distinct from source_modified_at

**Artifact:** `sage/sage_core_api.openapi.yaml` (Document.created_at, Document.source_modified_at)
**Category:** ingestion, provenance
**Decision:** `created_at` records when SAGE first processed the document.
`source_modified_at` records the source file's filesystem mtime. The two
fields serve different purposes and are set independently.

**Precondition:** Source file with mtime set to a date well in the past
(e.g., 2020-01-01).

**Input:** `ingest(source="old_file.md", adapter="markdown")`

**Expected:**
- `doc.created_at` is close to the current time (within 5 seconds)
- `doc.source_modified_at` matches the file's old mtime
- `doc.created_at != doc.source_modified_at`

**Rationale:** `created_at` as ingestion timestamp is useful for debugging
batch import issues. `source_modified_at` is the provenance signal for
content chronology. Conflating the two would lose one of those signals.


---

## Schema Changes Required

These design decisions require modifications to the Formal Substrate:

1. **Document.indexed_at** -- change from required to nullable (remove from `required` list, add `nullable: true`).
2. **Document.pipeline_error** -- add nullable string field. Populated when `pipeline_status: failed`.
3. **Edge.id** -- add required string field, auto-generated at creation.
4. **TraversalNode.edge_count** -- add integer field (default: 1).
5. **IngestRequest.force** -- add boolean field (default: false).
6. **set_lifecycle 200 response** -- add optional `warnings` array of strings.
7. **Vault config** -- add `retrieval_health.assertions_file` optional string field.
8. **PipelineStatus enum** -- no change needed; `abstraction_skipped` semantics are clarified
   by test assertions but the enum value is unchanged.
9. **Document.source_modified_at** -- add nullable date-time field. Source file mtime extracted by adapter at ingestion.
10. **Document.document_date** -- add nullable string field (YYYY-MM-DD format). Authoritative content date derived from filename or source_modified_at fallback.


---

## Search and Retrieval: Title Indexing and Keyword Mode

### TEST-SAGE-BH-058: Document identity signals indexed in chunk content for search

**Artifact:** Search bug report (2026-04-09): keyword search for "PV07" found
workflow documents referencing the term in body text but missed the actual PV07
patent drafts. The PV07 documents had titles like "ClinicalNormalization"
(extracted from first heading) while "PV07" appeared only in the source
filename and tags.

**Category:** ingestion / retrieval

**Decision:** The ingestion pipeline builds a search preamble from the document
record (title, source filename stem, tags) and prepends it to the first chunk's
content. This makes document identity signals discoverable via BM25 keyword
search and vector similarity without requiring a separate metadata search path.

**Precondition:** A document with title "ClinicalNormalization", source_path
containing "PV07", and tags including "PV07". The body content does not contain
the string "PV07".

**Input:** `discover(mode="semantic", query="PV07", use_hybrid=True)`

**Expected:**
- The document appears in search results (BM25 matches "PV07" in the source
  filename within the first chunk's preamble).

**Rationale:** Document identity lives in multiple places: title (from content),
source filename (from the filesystem), and tags/codes (from the filename
parser). All three must be searchable. Relying solely on body content misses
documents whose identifying codes appear only in the filename.


### TEST-SAGE-BH-059: Keyword-only retrieval mode uses BM25 without embedding

**Artifact:** Search bug report (2026-04-09): the frontend offered a "Keyword"
search mode that was never dispatched to the backend.

**Category:** retrieval

**Decision:** A new `keyword` retrieval mode runs BM25-only search. It does
not require a query embedding, making it faster and more predictable for exact
term matches.

**Precondition:** Two indexed documents. One contains the query term; the
other does not.

**Input:** `discover(mode="keyword", query="PV07")`

**Expected:**
- Returns only the document whose chunk content contains the query term.
- No embedding call is made (pure BM25).
- Response mode is `keyword`.

**Rationale:** Users selecting "Keyword" in the UI expect exact term matching.
Routing keyword mode through the semantic pipeline adds latency (embedding)
and noise (vector similarity to unrelated content).


### TEST-SAGE-BH-060: Keyword mode requires query field

**Category:** retrieval

**Precondition:** None.

**Input:** `discover(mode="keyword")` (no query)

**Expected:** Raises `MissingFieldError` for the `query` field.

**Rationale:** Keyword search without a query is meaningless.


### TEST-SAGE-BH-061: Keyword mode excludes failed-pipeline documents

**Category:** retrieval

**Decision:** Pipeline gating (BH-020) applies uniformly to all retrieval
modes, including the new keyword mode.

**Precondition:** A failed-pipeline document with chunk content matching the
query term.

**Input:** `discover(mode="keyword", query="matching term")`

**Expected:** The failed-pipeline document does not appear in results.

**Rationale:** Consistency. Failed-pipeline documents are quarantined from
all retrieval modes, not just semantic.


---

## Document Date Metadata

### TEST-SAGE-BH-062: Ingestion with filename date sets document_date from metadata

**Artifact:** `sage/sage_core_api.openapi.yaml` (Document.document_date)
**Category:** ingestion, provenance

**Decision:** When the caller supplies a `date` key in IngestRequest.metadata
(populated by the filename parser from a YYYY-MM-DD pattern in the filename),
the ingestion service stores it as `document_date` on the Document. This is
the authoritative content date, distinct from `source_modified_at` (filesystem
timestamp) and `created_at` (SAGE ingestion timestamp).

**Precondition:** SAGE vault initialized. Source file exists.

**Input:** `ingest(source="test.md", adapter="markdown", metadata={"date": "2026-04-10"})`

**Expected:**
- `doc.document_date == "2026-04-10"`
- `doc.source_modified_at` is set independently from the file's mtime
- `doc.document_date != doc.source_modified_at.date().isoformat()` (unless
  they happen to coincide)

**Rationale:** Filenames often encode the document's authoring or effective
date (e.g., `2026-04-10_PIM_PV07_checklist_v1.md`). This date is a stronger
provenance signal than the filesystem mtime, which can change from file
copies, restores, or cloud sync operations. Storing it as a dedicated field
preserves both signals without conflation.


### TEST-SAGE-BH-063: Ingestion without filename date falls back to source_modified_at date

**Artifact:** `sage/sage_core_api.openapi.yaml` (Document.document_date)
**Category:** ingestion, provenance

**Decision:** When no `date` key is present in IngestRequest.metadata, the
ingestion service derives `document_date` from the date portion of
`source_modified_at` (YYYY-MM-DD). This ensures every file-sourced document
has a document_date.

**Precondition:** SAGE vault initialized. Source file exists with a known
modification time (set via `os.utime` for determinism, e.g., 2025-06-15).

**Input:** `ingest(source="PIM_PV07_checklist_v1.md", adapter="markdown")`
(no `date` key in metadata)

**Expected:**
- `doc.document_date == "2025-06-15"` (date portion of source_modified_at)
- `doc.source_modified_at` is set to the file's full mtime datetime

**Rationale:** Files without date codes in their filename still have a
meaningful content date: the last time the source file was modified. Using
the date portion of source_modified_at as fallback ensures document_date
is populated for all file-sourced documents, supporting chronological
sorting and display without requiring filename conventions.


### TEST-SAGE-BH-064: Ingestion with no filename date and no source_modified_at leaves document_date null

**Artifact:** `sage/sage_core_api.openapi.yaml` (Document.document_date)
**Category:** ingestion, provenance

**Decision:** When neither a filename date nor source_modified_at is
available, document_date remains null. This covers non-file sources or
adapters that do not provide filesystem metadata.

**Precondition:** SAGE vault initialized. Stub adapter that returns no
`source_modified_at` in ProjectionResult.metadata.

**Input:** `ingest(source="api_content", adapter="markdown")` with no `date`
in metadata and adapter providing no source_modified_at.

**Expected:**
- `doc.document_date is None`
- `doc.source_modified_at is None`

**Rationale:** Null is a valid state. Forcing a synthetic date (e.g.,
created_at) would conflate SAGE operational timestamps with content
provenance. Better to leave it null and let the UI indicate "unknown."


### TEST-SAGE-BH-065: document_date round-trips through graph store

**Artifact:** `sage/storage/graph_store.py` (insert_document, get_document)
**Category:** graph_store, serialization

**Decision:** `document_date` is stored as a TEXT column in SQLite containing
a YYYY-MM-DD string. Unlike datetime fields (source_modified_at, created_at),
no ISO 8601 datetime parsing is needed -- it is a pure date string.

**Precondition:** SAGE vault initialized.

**Input:** Insert a Document with `document_date="2026-04-10"`. Retrieve it
via `get_document`.

**Expected:**
- Retrieved `doc.document_date == "2026-04-10"`
- The value is a string, not a datetime object

**Rationale:** document_date has no time component. Storing it as a date
string avoids unnecessary datetime parsing and timezone considerations. The
round-trip test guards the SQLite TEXT serialization path for this field,
following the same pattern as BH-051 for source_modified_at.

### TEST-SAGE-BH-066: Hash-only duplicate detection across different paths

**Artifact:** `sage/services/ingestion.py` (ingest method)
**Category:** ingestion, duplicate_detection
**Decision:** Same content hash at a different source_path is also a duplicate.

**Precondition:** Document A ingested from `patents/doc_a.docx` with hash H.

**Input:** Ingest from `patents/subfolder/doc_a_copy.docx` with identical
content (hash H), no force flag.

**Expected:**
- DuplicateContentError raised (HTTP 409)
- `code: "duplicate_content"`
- `detail.existing_document_id: <doc_A_id>`
- `detail.source_content_hash: H`

**Rationale:** Content-identical files at different paths are common in messy
real-world file systems (renamed copies, files moved between folders). Admitting
a second copy creates unwanted duplicates that complicate edge curation and
retrieval. The hash-only check catches these before the path+hash check.

### TEST-SAGE-BH-067: Force re-ingestion reuses existing document at different path

**Artifact:** `sage/services/ingestion.py` (ingest method, force flag)
**Category:** ingestion, duplicate_detection
**Decision:** `force: true` with identical content hash reuses the existing
document record regardless of source path. The content hash is the identity
signal; the path is mutable metadata.

**Precondition:** Document A ingested from `patents/doc_a.docx` with hash H.

**Input:** Ingest from `patents/subfolder/doc_a_copy.docx` with identical
content (hash H), `force: true`.

**Expected:**
- Existing document record reused (same document ID as doc_A_id)
- `source_path` updated to `patents/subfolder/doc_a_copy.docx`
- `is_new` is False
- Pipeline re-runs (semantic_abstract cleared, content store re-indexed)

**Rationale:** The content hash uniquely identifies document content. When a
file moves to a new path, force re-ingestion should update the path on the
existing record rather than creating a duplicate. This keeps edges, metadata,
and document identity stable across file reorganizations.

### TEST-SAGE-BH-068: Sequential pipeline sets final status before returning

**Artifact:** SAGE ingestion pipeline, sequential execution model
**Category:** ingestion, execution, memory
**Decision:** `ingest()` awaits the full pipeline (projection, indexing,
abstraction) and re-fetches the document before returning, so the caller
receives the terminal pipeline status without polling.

**Precondition:** SAGE vault initialized with abstraction enabled.

**Input:** Ingest a document. Inspect the returned `IngestResult.document`
immediately (no sleep, no polling).

**Expected:**
- `pipeline_status` is `"abstraction_complete"`
- `indexed_at` is not null
- `semantic_abstract` is not null
- The graph store document matches the returned document

**Rationale:** Verifies the sequential pipeline contract end-to-end: callers
can trust the returned document reflects completed processing. Eliminates the
race conditions inherent in the background-task model.


---

## Salience Reranking

### TEST-SAGE-BH-069: Active lifecycle tier sort in semantic retrieval

**Artifact:** `sage/services/retrieval.py` (_rerank_salience)
**Category:** retrieval, salience

**Decision:** Documents with `lifecycle_status="active"` always rank above
non-active documents in semantic and keyword retrieval modes, regardless of
content relevance score. Salience reranking sorts by (lifecycle_tier, score)
where active = tier 0 and all other statuses = tier 1. Agents that need
superseded or archived versions traverse the supersedes chain from the active
head rather than relying on search ranking.

**Precondition:** Two documents indexed. Doc A: `lifecycle_status="active"`,
lower content score. Doc B: `lifecycle_status="archived"`, higher content score.

**Input:** `discover(mode="semantic", query="matching query")`

**Expected:**
- Both documents appear in results.
- Doc A ranks above Doc B despite Doc B having a higher content score.

**Rationale:** For code-based lookups (the dominant retrieval pattern), every
version of a document matches equally on content, metadata, and abstract. A
multiplicative boost was insufficient to guarantee the active version ranked
first. Agents retrieve historical versions via supersedes chain traversal,
so search ranking does not need to preserve that capability.


### TEST-SAGE-BH-070: Recency boost in semantic retrieval

**Artifact:** `sage/services/retrieval.py` (_rerank_salience)
**Category:** retrieval, salience

**Decision:** Documents with recent dates receive up to a 1.10x score multiplier
via exponential decay with a 365-day half-life. Date resolution priority:
`document_date` > `source_modified_at`. Documents with no date receive no
recency boost.

**Precondition:** Two documents indexed with identical content relevance.
Doc A: `document_date="2026-04-01"` (recent). Doc B: `document_date="2020-01-01"` (old).

**Input:** `discover(mode="semantic", query="matching query")`

**Expected:**
- Both documents appear in results.
- Doc A scores higher than Doc B (recency boost applied).

**Rationale:** Recency is a weak relevance signal in personal knowledge bases
where newer documents tend to be more actionable. The half-life is long (365 days)
to avoid penalizing reference material.


### TEST-SAGE-BH-071: Same-name same-content re-import returns existing path

**Artifact:** `sage/services/ingestion.py` (_ensure_vault_local)
**Category:** ingestion, deduplication

**Decision:** When an external file is imported and a file with the same name
and identical content hash already exists in the vault, return the existing
path without copying. No hash-suffixed duplicate file is created.

**Precondition:** Document previously ingested from an external path; the imported
copy exists in the vault's sources directory.

**Input:** Re-import the same external file (same name, same content).

**Expected:**
- Returns the path to the existing file in the vault.
- No new file is created on disk.

**Rationale:** Prevents orphaned hash-suffixed duplicates that accumulate during
repeated imports of unchanged files.


---

## Catalog Mode and Hard Tag Filtering

### TEST-SAGE-BH-072: Catalog mode returns all documents matching filters

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, catalog mode)
**Category:** retrieval

**Decision:** Catalog mode queries the SQLite documents table directly with
filter predicates, bypassing vector/BM25 search entirely. Returns document-level
metadata only. No query string required.

**Precondition:** Vault with 5 documents:
- doc_a: `doc_type="patent_draft"`, `lifecycle_status="active"`, `tags=["PV07"]`
- doc_b: `doc_type="patent_draft"`, `lifecycle_status="active"`, `tags=["PV08"]`
- doc_c: `doc_type="glossary"`, `lifecycle_status="active"`, `tags=["PV07"]`
- doc_d: `doc_type="patent_draft"`, `lifecycle_status="archived"`, `tags=["PV07"]`
- doc_e: `doc_type="checklist"`, `lifecycle_status="active"`, `tags=["PV07"]`

**Input:** `discover(mode="catalog", scope="filtered", filters={"doc_type": "patent_draft"})`

**Expected:**
- Results contain exactly doc_a, doc_b, doc_d (all patent_draft documents).
- `total_available == 3`
- Response mode is `catalog`.

**Rationale:** Catalog mode provides a deterministic, filter-only retrieval path
that eliminates the need for semantic search workarounds when enumerating
documents by metadata.


### TEST-SAGE-BH-073: Catalog mode pagination (limit + offset)

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, catalog mode)
**Category:** retrieval

**Decision:** Catalog mode supports pagination via `limit` and `offset` parameters.
`total_available` reflects the full unpaged count, enabling callers to compute
page counts and detect whether more results exist.

**Precondition:** Vault with 5 non-failed documents (doc_a through doc_e as above).

**Input:** `discover(mode="catalog", limit=2, offset=0)` then
`discover(mode="catalog", limit=2, offset=2)`

**Expected:**
- First request: 2 results, `total_available == 5`.
- Second request: 2 results, `total_available == 5`.
- No overlap between the two result sets.
- Union of both result sets plus a third request at offset=4 covers all 5 documents.

**Rationale:** Pagination prevents unbounded result sets for large vaults and
enables the Cowork agent pattern of iterating through all documents in fixed-size
pages.


### TEST-SAGE-BH-074: Catalog mode tag filtering is deterministic

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, catalog mode)
**Category:** retrieval

**Decision:** Tags filter in catalog mode is a hard SQL constraint, not a relevance
signal. Every returned document has ALL specified tags. Documents missing any
specified tag are excluded, regardless of other matching criteria.

**Precondition:** Vault with 5 documents (as in BH-072). Tags are JSON arrays
stored in SQLite.

**Input:** `discover(mode="catalog", scope="filtered", filters={"tags": ["PV07"]})`

**Expected:**
- Results contain exactly doc_a, doc_c, doc_d, doc_e (all PV07-tagged documents).
- `total_available == 4`
- doc_b (tagged PV08, not PV07) is excluded.

**Rationale:** The primary motivation for catalog mode. Semantic search cannot
reliably enumerate by tag because tags live in the graph store, not the vector
index. SQL-based tag filtering guarantees completeness.


### TEST-SAGE-BH-075: Catalog mode returns no chunk content or relevance scores

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, catalog mode)
**Category:** retrieval

**Decision:** Catalog mode returns document-level metadata only. DiscoverHit
fields `chunk_content`, `heading_path`, and `relevance_score` are all null.

**Precondition:** Vault with at least 1 indexed document.

**Input:** `discover(mode="catalog")`

**Expected:**
- Every hit has `chunk_content is None`, `heading_path is None`, `relevance_score is None`.
- Every hit has a populated `document` field with id, title, lifecycle_status,
  source_type, version_label, project, doc_type, tags.

**Rationale:** Catalog mode serves enumeration, not content retrieval. Returning
chunk content would be wasteful and misleading (no search was performed to select
relevant chunks).


### TEST-SAGE-BH-076: Catalog mode excludes failed pipeline documents

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, catalog mode)
**Category:** retrieval

**Decision:** Pipeline gating (BH-020) applies uniformly to all retrieval modes,
including catalog. Failed-pipeline documents are excluded from catalog results.

**Precondition:** Vault with 2 documents:
- doc_a: `pipeline_status="abstraction_complete"` (healthy)
- doc_b: `pipeline_status="failed"` (quarantined)

**Input:** `discover(mode="catalog")`

**Expected:**
- Results contain doc_a only.
- `total_available == 1`
- doc_b is excluded.

**Rationale:** Consistency with BH-020 and BH-061. Callers can use
`filters={"pipeline_status": "failed"}` if they explicitly want to find
quarantined documents.


### TEST-SAGE-BH-077: Catalog mode with no filters returns all non-failed documents

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, catalog mode)
**Category:** retrieval

**Decision:** Catalog mode with no filters and default scope ("all") returns
every non-failed document in the vault. This is the "list everything" use case.

**Precondition:** Vault with 5 non-failed documents and 1 failed document.

**Input:** `discover(mode="catalog")`

**Expected:**
- Results contain exactly 5 documents (all non-failed).
- `total_available == 5`

**Rationale:** A filter-free catalog call is the simplest way to get a complete
vault inventory. Combined with pagination, it replaces the `keyword` mode
`query="*"` workaround.


### TEST-SAGE-BH-078: Catalog mode total_available reflects full count, not page size

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, catalog mode)
**Category:** retrieval

**Decision:** `total_available` is the exact count of all matching documents
(after filter and pipeline exclusion), independent of `limit` and `offset`.

**Precondition:** Vault with 10 non-failed documents.

**Input:** `discover(mode="catalog", limit=3, offset=0)`

**Expected:**
- `len(results) == 3`
- `total_available == 10`

**Rationale:** Callers need the total count to compute page numbers and determine
whether more pages exist. Unlike semantic mode where total_available is an
approximation, catalog mode provides an exact count from SQL COUNT(*).


### TEST-SAGE-BH-079: Catalog mode combined filters (doc_type + tags + lifecycle_status)

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, catalog mode)
**Category:** retrieval

**Decision:** Multiple filter fields are combined with AND semantics. A document
must match ALL specified filters to appear in results.

**Precondition:** Vault with 5 documents (as in BH-072).

**Input:** `discover(mode="catalog", scope="filtered", filters={"doc_type": "patent_draft", "tags": ["PV07"], "lifecycle_status": "active"})`

**Expected:**
- Results contain exactly doc_a (the only active patent_draft tagged PV07).
- `total_available == 1`

**Rationale:** AND semantics are the natural interpretation for metadata filters.
OR semantics would require explicit combinators and are not needed for the
primary use cases (drill-down from dashboard, tag-based enumeration).


## Document-Level Response Mode

### TEST-SAGE-BH-084: Semantic search with response_level=documents omits chunk content

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, response_level)
**Category:** retrieval

**Decision:** When `response_level="documents"`, semantic search returns one
`DiscoverHit` per matched document with `chunk_content=None`. The `heading_path`
of the best-scoring chunk is preserved as cheap "why this matched" context.
The `document` summary, `relevance_score`, and `matched_chunk_count` are preserved.
This reduces payload size for callers that need ranked document lists without
chunk text.

**Precondition:** Vault with 3 indexed documents (doc_a, doc_b, doc_c),
each with at least one chunk containing the word "integration".

**Input:** `discover(mode="semantic", query="integration", response_level="documents")`

**Expected:**
- All returned hits have `chunk_content is None`.
- All returned hits have `heading_path` (not None -- best chunk's heading preserved).
- All returned hits have `relevance_score` > 0.
- All returned hits have `matched_chunk_count >= 1`.
- Each hit's `document` field contains a valid `DocumentSummary` with `id`, `title`, etc.
- Result count matches the number of distinct documents matching the query.

**Rationale:** MCP callers and dashboard drill-downs often need ranked document
lists for navigation. Transmitting chunk text wastes bandwidth and context window
when the caller will fetch document details separately. The heading path provides
a one-line "why this matched" without the text payload.


### TEST-SAGE-BH-085: Keyword search with response_level=documents omits chunk content

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, response_level)
**Category:** retrieval

**Decision:** `response_level="documents"` applies to keyword mode identically
to semantic mode. BM25 results are deduplicated by document (existing behavior)
and `chunk_content` is suppressed. `heading_path` and `matched_chunk_count`
are preserved.

**Precondition:** Vault with 3 indexed documents, each containing the word "protocol".

**Input:** `discover(mode="keyword", query="protocol", response_level="documents")`

**Expected:**
- All returned hits have `chunk_content is None`.
- All returned hits have `heading_path` (not None).
- All returned hits have `relevance_score` > 0.
- All returned hits have `matched_chunk_count >= 1`.
- Each hit's `document` field is a valid `DocumentSummary`.

**Rationale:** Keyword mode shares the same `_results_to_hits()` pipeline as
semantic mode; `response_level` should apply uniformly to both search modes.


### TEST-SAGE-BH-086: response_level=documents preserves relevance scores and ordering

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, response_level)
**Category:** retrieval

**Decision:** Document-level responses preserve the same relevance scores,
result ordering, heading paths, and matched chunk counts as chunk-level
responses. The only difference is the suppression of `chunk_content`.

**Precondition:** Vault with 3 indexed documents with varying relevance to
the query "claim construction methodology".

**Input:**
1. `discover(mode="semantic", query="claim construction methodology", response_level="chunks")`
2. `discover(mode="semantic", query="claim construction methodology", response_level="documents")`

**Expected:**
- Both responses return the same documents in the same order.
- Both responses have the same `relevance_score` for each document.
- Response 1 has non-null `chunk_content` on each hit.
- Response 2 has null `chunk_content` on each hit.

**Rationale:** `response_level` is a presentation concern, not a retrieval
concern. Changing the response shape must not alter scoring or ranking behavior.


### TEST-SAGE-BH-087: response_level=chunks (default) preserves current behavior

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, response_level)
**Category:** retrieval

**Decision:** `response_level` defaults to `"chunks"`, preserving backward
compatibility. Omitting the parameter produces the same response shape as
before the feature was added.

**Precondition:** Vault with at least 1 indexed document containing searchable content.

**Input:** `discover(mode="semantic", query="integration")` (no response_level specified)

**Expected:**
- At least one hit has non-null `chunk_content`.
- At least one hit has non-null `heading_path`.
- Behavior is identical to an explicit `response_level="chunks"`.

**Rationale:** Backward compatibility. Existing callers (frontend Search view,
MCP tool) must not be affected by the addition of this parameter.


### TEST-SAGE-BH-088: response_level ignored by catalog mode

**Artifact:** `sage/sage_core_api.openapi.yaml` (discover, response_level)
**Category:** retrieval

**Decision:** Catalog mode always returns document-level metadata without
chunk content, regardless of the `response_level` value. The parameter is
accepted but has no effect.

**Precondition:** Vault with 3 non-failed documents.

**Input:** `discover(mode="catalog", response_level="chunks")`

**Expected:**
- All returned hits have `chunk_content is None`.
- All returned hits have `heading_path is None`.
- All returned hits have `relevance_score is None`.
- Response is identical to `discover(mode="catalog")` without `response_level`.

**Rationale:** Catalog mode operates on the SQLite documents table and never
touches the content store. Applying `response_level` would be a no-op;
accepting the parameter silently avoids forcing callers to branch on mode.


---

## Chain Walk

### TEST-SAGE-BH-089: Linear supersedes chain returns ordered version history

**Artifact:** `sage/sage_core_api.openapi.yaml` (chain endpoint)
**Category:** graph, chain
**Decision:** `sage_chain` walks to both ends of a linear edge chain from any
starting node via recursive CTE, returning an ordered list with positional
metadata.

**Precondition:** 5 documents forming a linear supersedes chain:
v1 <- v2 <- v3 <- v4 <- v5 (each supersedes its immediate predecessor).

**Input:** `chain(document_id: v3.id, edge_type: "supersedes")`

**Expected:**
- `chain` list has 5 entries ordered by position (0=v1 tail, 4=v5 head).
- `head_id` = v5.id.
- `tail_id` = v1.id.
- `query_position` = 2.
- `length` = 5.
- `is_linear` = true.
- Each entry has `id`, `title`, `version_label`, `lifecycle_status`, `document_date`.

**Rationale:** The primary use case for supersedes chains is "show me the version
history." An ordered list with head/tail/position is the natural response shape.
One recursive CTE round-trip regardless of chain length.


### TEST-SAGE-BH-090: Chain walk from head document

**Artifact:** `sage/sage_core_api.openapi.yaml` (chain endpoint)
**Category:** graph, chain

**Decision:** Chain walk produces the same ordered result regardless of which
node the caller starts from.

**Precondition:** Same 5-document supersedes chain as BH-089.

**Input:** `chain(document_id: v5.id, edge_type: "supersedes")`

**Expected:**
- Same 5-entry chain as BH-089 (identical ordering and content).
- `query_position` = 4 (head position).
- `head_id` = v5.id, `tail_id` = v1.id.

**Rationale:** Callers should not need to know whether they hold a head, tail,
or middle document to get the full chain. Any node is a valid entry point.


### TEST-SAGE-BH-091: Chain walk from tail document

**Artifact:** `sage/sage_core_api.openapi.yaml` (chain endpoint)
**Category:** graph, chain

**Decision:** Same invariant as BH-090: entry point does not affect result.

**Precondition:** Same 5-document supersedes chain as BH-089.

**Input:** `chain(document_id: v1.id, edge_type: "supersedes")`

**Expected:**
- Same 5-entry chain as BH-089.
- `query_position` = 0 (tail position).

**Rationale:** Confirms both-direction walking from the tail end.


### TEST-SAGE-BH-092: Single-node chain (no edges of requested type)

**Artifact:** `sage/sage_core_api.openapi.yaml` (chain endpoint)
**Category:** graph, chain
**Decision:** A document with no edges of the requested type returns a
single-entry chain (the document itself). This is a valid result, not an error.

**Precondition:** doc_a exists with no supersedes edges (may have other edge types).

**Input:** `chain(document_id: doc_a.id, edge_type: "supersedes")`

**Expected:**
- `chain` has 1 entry: doc_a.
- `head_id` = `tail_id` = doc_a.id.
- `query_position` = 0.
- `length` = 1.
- `is_linear` = true.

**Rationale:** Every document is trivially a chain of length 1. Returning an
error for "no chain found" would force callers to handle two code paths
(chain vs standalone). A single-entry chain is the degenerate case.


### TEST-SAGE-BH-093: Fork detection sets is_linear false

**Artifact:** `sage/sage_core_api.openapi.yaml` (chain endpoint)
**Category:** graph, chain
**Decision:** When the edge graph has a fork (multiple documents supersede the
same predecessor, or a document has multiple predecessors), `is_linear` is false.
All reachable documents are still returned. This is a data quality signal, not
an error.

**Precondition:** doc_a <- doc_b (supersedes), doc_a <- doc_c (supersedes).
Two documents claim to supersede the same predecessor.

**Input:** `chain(document_id: doc_a.id, edge_type: "supersedes")`

**Expected:**
- `is_linear` = false.
- All 3 reachable documents included in `chain`.
- `length` = 3.

**Rationale:** Forked chains indicate a data quality issue (accidental duplicate
import, conflicting version claims). Reporting the fork via `is_linear` lets
callers surface the problem rather than silently returning a partial chain.
The chain integrity summary (step 34) will add richer fork diagnostics.


### TEST-SAGE-BH-094: Chain ignores other edge types

**Artifact:** `sage/sage_core_api.openapi.yaml` (chain endpoint)
**Category:** graph, chain
**Decision:** Chain walk follows only the specified edge type. Other edge types
connecting to chain members are invisible to the walk.

**Precondition:** doc_a supersedes doc_b. doc_a also has a `covers` edge to doc_c.

**Input:** `chain(document_id: doc_a.id, edge_type: "supersedes")`

**Expected:**
- `chain` contains exactly doc_b and doc_a (length 2).
- doc_c is not present.

**Rationale:** Edge-type isolation is fundamental to chain semantics. A version
history must not include documents connected by unrelated relationship types.


### TEST-SAGE-BH-095: Chain with non-existent document returns 404

**Artifact:** `sage/sage_core_api.openapi.yaml` (chain endpoint)
**Category:** graph, chain, error
**Decision:** Standard document-not-found error for invalid starting document.

**Input:** `chain(document_id: "nonexistent", edge_type: "supersedes")`

**Expected:**
- HTTP 404.
- `code: "document_not_found"`.

**Rationale:** Consistent with traverse endpoint error handling (BH-037 precondition
validation pattern).


### TEST-SAGE-BH-096: Chain works with non-supersedes edge types

**Artifact:** `sage/sage_core_api.openapi.yaml` (chain endpoint)
**Category:** graph, chain
**Decision:** `sage_chain` is edge-type-generic. Any edge type that forms a
linear chain can be walked. The `edge_type` parameter is required (no default).

**Precondition:** doc_a -> doc_b -> doc_c via `references` edges (outbound direction).

**Input:** `chain(document_id: doc_b.id, edge_type: "references")`

**Expected:**
- Ordered chain of 3 documents.
- `is_linear` = true.
- `head_id` = doc_c.id, `tail_id` = doc_a.id.
- `query_position` = 1.

**Rationale:** While the primary motivator is supersedes chains, the operation
is structurally generic. Restricting it to supersedes would be an artificial
limitation that prevents callers from walking other linear structures
(e.g., depends_on chains).


---

## Edge-Type Breakdown on Traverse

### TEST-SAGE-BH-097: edge_counts map with mixed edge types

**Artifact:** `sage/sage_core_api.openapi.yaml` (TraversalNode.edge_counts)
**Category:** graph, traversal
**Decision:** `TraversalNode.edge_counts` is a `dict[str, int]` keyed by edge type,
replacing the scalar `edge_count` field. Each value is the count of edges of
that type connecting to the document from the traversal path. Breaking change
from `edge_count: int`.

**Precondition:** doc_a has 2 `supersedes` edges and 3 `covers` edges to doc_b
(all outbound from doc_a).

**Input:** `traverse(start_id: doc_a.id, direction: "outbound")`

**Expected:**
- doc_b appears once in results (deduplication preserved).
- `edge_counts: {"supersedes": 2, "covers": 3}`.
- No `edge_count` field present.

**Rationale:** The scalar `edge_count` gave a total but forced a second query
to understand the relationship composition. A per-type map answers "what kinds
of relationships exist and how many of each?" in one response. This is the
information the Graph Explorer needs for edge-type breakdown display.


### TEST-SAGE-BH-098: Single edge type produces single-key map

**Artifact:** `sage/sage_core_api.openapi.yaml` (TraversalNode.edge_counts)
**Category:** graph, traversal

**Decision:** When only one edge type connects to a document, `edge_counts`
has a single key. The map shape is consistent regardless of edge diversity.

**Precondition:** doc_a has 1 `references` edge to doc_b.

**Input:** `traverse(start_id: doc_a.id, direction: "outbound")`

**Expected:**
- `edge_counts: {"references": 1}`.

**Rationale:** No special-casing for single-type connections. Callers always
receive a map and can iterate it uniformly.


### TEST-SAGE-BH-099: Traversal with edge_type filter shows only filtered type in counts

**Artifact:** `sage/sage_core_api.openapi.yaml` (TraversalNode.edge_counts)
**Category:** graph, traversal

**Decision:** When `traverse()` is called with an `edge_type` filter,
`edge_counts` contains only the filtered type(s). Edges of other types are
not traversed, so they do not appear in counts.

**Precondition:** doc_a has 2 `supersedes` and 3 `covers` edges to doc_b.

**Input:** `traverse(start_id: doc_a.id, edge_type: "supersedes", direction: "outbound")`

**Expected:**
- doc_b node has `edge_counts: {"supersedes": 2}`.
- No `covers` key present in `edge_counts`.

**Rationale:** The CTE only follows edges matching the type filter. Counts
reflect what the traversal actually walked, not the full edge inventory of
the target document.


### TEST-SAGE-BH-100: Multi-depth traversal accumulates per-node edge_counts independently

**Artifact:** `sage/sage_core_api.openapi.yaml` (TraversalNode.edge_counts)
**Category:** graph, traversal

**Decision:** Each `TraversalNode` carries its own `edge_counts` reflecting the
edges that reached that specific document during traversal. Counts are scoped
to the node, not aggregated across the graph.

**Precondition:** doc_a -> doc_b (1 `supersedes`, 2 `covers` edges),
doc_b -> doc_c (3 `references` edges).

**Input:** `traverse(start_id: doc_a.id, direction: "outbound", depth: 2)`

**Expected:**
- doc_b has `edge_counts: {"supersedes": 1, "covers": 2}`.
- doc_c has `edge_counts: {"references": 3}`.

**Rationale:** Per-node scoping ensures `edge_counts` describes the local
relationship structure at each document. Callers can inspect any node's
connectivity without cross-referencing other nodes.


## Semantic Abstract Consumers (CAS-ADR-011)

These tests verify that semantic abstracts generated during ingestion Stage 3
are surfaced to retrieval consumers: steward agents, vault-steward discovery,
and (in Phase 2) two-pass abstract-boosted retrieval.


### TEST-SAGE-BH-101: Semantic discover returns semantic_abstract on DocumentSummary

**Artifact:** `sage/models/schemas.py` (DocumentSummary), `sage/services/retrieval.py`
**Category:** retrieval, abstraction

**Decision:** When a document has a `semantic_abstract`, the `DocumentSummary`
returned in every `DiscoverHit` must include it. This enables steward agents
and vault-steward discovery to access the abstract without a second
`sage_get_document` call.

**Precondition:** A document with `semantic_abstract` set to a non-empty string
is indexed with at least one chunk.

**Input:** `discover(mode: semantic, query: <matching query>)`

**Expected:**
- The matching `DiscoverHit.document.semantic_abstract` equals the stored abstract.

**Rationale:** CAS-ADR-011 identifies steward orientation and vault-steward
discovery as primary abstract consumers. Both read `DocumentSummary` from
discover results.


### TEST-SAGE-BH-102: Discover returns None abstract for abstraction-skipped documents

**Artifact:** `sage/models/schemas.py` (DocumentSummary), `sage/services/retrieval.py`
**Category:** retrieval, abstraction

**Decision:** Documents with `pipeline_status = abstraction_skipped` (no
abstract generated) must return `semantic_abstract = None` on their
`DocumentSummary`. The field is always present but nullable.

**Precondition:** A document with `pipeline_status = abstraction_skipped` and no
`semantic_abstract` is indexed with at least one chunk.

**Input:** `discover(mode: semantic, query: <matching query>)`

**Expected:**
- The matching `DiscoverHit.document.semantic_abstract` is `None`.

**Rationale:** Consumers must handle the absence of an abstract gracefully.
The nullable field avoids forcing consumers to check pipeline status before
accessing the abstract.


### TEST-SAGE-BH-103: Catalog mode returns semantic_abstract on DocumentSummary

**Artifact:** `sage/models/schemas.py` (DocumentSummary), `sage/services/retrieval.py`
**Category:** retrieval, abstraction, catalog

**Decision:** Catalog mode (filter-only document enumeration) includes
`semantic_abstract` on each `DocumentSummary`, consistent with all other
retrieval modes.

**Precondition:** Two documents exist: one with a `semantic_abstract`, one without.

**Input:** `discover(mode: catalog, limit: 10)`

**Expected:**
- The document with an abstract has `semantic_abstract` equal to its stored value.
- The document without an abstract has `semantic_abstract = None`.

**Rationale:** Catalog mode is the primary discovery surface for the CAS
dashboard. Agents browsing the catalog should see abstracts inline.


### TEST-SAGE-BH-104: Document-level response mode preserves semantic_abstract

**Artifact:** `sage/models/schemas.py` (DocumentSummary), `sage/services/retrieval.py`
**Category:** retrieval, abstraction, response_level

**Decision:** When `response_level = documents` (chunk content suppressed),
`semantic_abstract` is still present on the `DocumentSummary`. The abstract is
document-level metadata, not chunk content, so it is never suppressed.

**Precondition:** A document with `semantic_abstract` is indexed with chunks.

**Input:** `discover(mode: semantic, query: <matching query>, response_level: documents)`

**Expected:**
- `DiscoverHit.chunk_content` is `None` (suppressed by response_level).
- `DiscoverHit.document.semantic_abstract` equals the stored abstract (preserved).

**Rationale:** The `response_level=documents` mode exists for bandwidth
optimization when callers need only document identity and metadata. The
abstract is metadata, not content, so suppressing it would defeat its purpose
as an orientation aid.


### TEST-SAGE-BH-105: Abstract prefilter boosts documents whose abstract matches query

**Artifact:** `sage/services/retrieval.py`, `sage/storage/graph_store.py`
**Category:** retrieval, abstraction, two-pass

**Decision:** When `use_abstract_prefilter` is true (default), documents whose
`semantic_abstract` contains query terms receive a score boost above documents
whose abstract does not match. This implements the two-pass retrieval pattern
from CAS-ADR-011.

**Precondition:** Two documents with identical chunk content. Document A has a
`semantic_abstract` containing the query terms. Document B has a
`semantic_abstract` that does not contain the query terms.

**Input:** `discover(mode: semantic, query: <terms matching A's abstract>)`

**Expected:**
- Both documents appear in results.
- Document A ranks above Document B.

**Rationale:** CAS-ADR-011 identifies two-pass retrieval as the primary
architectural consumer: abstract search filters for relevant documents before
chunk search locates specific content. The boost (not hard filter) ensures
documents with poor abstracts but strong chunk matches are not suppressed.


### TEST-SAGE-BH-106: Abstract prefilter does not exclude documents without abstracts

**Artifact:** `sage/services/retrieval.py`
**Category:** retrieval, abstraction, two-pass

**Decision:** Documents with `semantic_abstract = None` (abstraction skipped or
failed) are never excluded by the abstract prefilter. They remain eligible
from chunk search at their natural relevance score.

**Precondition:** Two documents: one with a matching abstract and indexed chunks,
one with `pipeline_status = abstraction_skipped` (no abstract) and indexed
chunks containing query terms.

**Input:** `discover(mode: semantic, query: <terms present in both documents' chunks>)`

**Expected:**
- Both documents appear in results.
- The abstractless document is present (not excluded).

**Rationale:** Graceful degradation. Abstracts are an enrichment, not a gate.
Documents ingested before abstraction was enabled, or where generation failed,
must remain discoverable.


### TEST-SAGE-BH-107: Abstract prefilter respects scope gating

**Artifact:** `sage/services/retrieval.py`
**Category:** retrieval, abstraction, scope

**Decision:** Documents matched by abstract search are still subject to scope
gating. A document whose abstract matches the query but fails the scope
filter does not receive a boost.

**Precondition:** Two documents with abstracts matching the query. Document A
has `authority_scope` set. Document B does not.

**Input:** `discover(mode: semantic, query: <matching>, scope: authoritative)`

**Expected:**
- Document A appears in results (passes authoritative scope).
- Document B does not appear (fails authoritative scope, regardless of abstract match).

**Rationale:** Scope gating is a security and governance boundary. Abstract
relevance must not override access control or scope restrictions.


### TEST-SAGE-BH-108: use_abstract_prefilter=False disables abstract boost

**Artifact:** `sage/services/retrieval.py`, `sage/models/schemas.py`
**Category:** retrieval, abstraction, configuration

**Decision:** Setting `use_abstract_prefilter = False` on `DiscoverRequest`
disables the abstract boost entirely. Documents are ranked by chunk relevance
alone (plus existing salience and metadata boosts).

**Precondition:** Two documents. Document A has a strongly matching abstract.
Document B has identical chunk content but no matching abstract.

**Input:** `discover(mode: semantic, query: <matching A's abstract>, use_abstract_prefilter: false)`

**Expected:**
- Both documents appear with similar scores (no abstract-derived ordering advantage for A).

**Rationale:** Opt-out is essential for callers who want pure content relevance
or for diagnostic comparison between boosted and unboosted rankings.


### TEST-SAGE-BH-109: Keyword mode benefits from abstract prefilter

**Artifact:** `sage/services/retrieval.py`
**Category:** retrieval, abstraction, keyword

**Decision:** The abstract prefilter applies to keyword (BM25) mode as well as
semantic mode. The boost mechanism is identical.

**Precondition:** Two documents with indexed chunks. Document A has a
`semantic_abstract` containing the query terms. Document B does not.

**Input:** `discover(mode: keyword, query: <terms matching A's abstract>)`

**Expected:**
- Document A ranks above Document B.

**Rationale:** Keyword search is the fallback for queries where embedding
similarity is unreliable. Abstract orientation is equally valuable in both modes.


### TEST-SAGE-BH-110: Abstract prefilter integrates with hybrid RRF

**Artifact:** `sage/services/retrieval.py`
**Category:** retrieval, abstraction, hybrid

**Decision:** When hybrid RRF is active (`use_hybrid=True`), the abstract boost
is applied after RRF fusion, before salience reranking. The abstract boost
and RRF scores compose multiplicatively.

**Precondition:** Two documents with indexed chunks. Document A has a matching
abstract. Document B does not.

**Input:** `discover(mode: semantic, query: <matching>, use_hybrid: true)`

**Expected:**
- Document A ranks above Document B (abstract boost applied after RRF fusion).

**Rationale:** RRF fusion produces a unified ranking from vector and BM25
signals. The abstract boost is an independent signal that should compose with
(not replace) the fused ranking.


### TEST-SAGE-BH-111: Abstract boost composes with lifecycle tier sort

**Artifact:** `sage/services/retrieval.py`
**Category:** retrieval, abstraction, salience

**Decision:** The abstract boost is applied before salience reranking. The
lifecycle tier sort (BH-069) then ensures active documents rank above non-active
documents regardless of abstract-boosted score magnitude.

**Precondition:** Two documents with matching abstracts. Document A has
`lifecycle_status = active`. Document B has `lifecycle_status = draft`.
Both have identical chunk content and abstract text.

**Input:** `discover(mode: semantic, query: <matching both abstracts>)`

**Expected:**
- Both documents receive the abstract boost.
- Document A ranks above Document B due to lifecycle tier sort (BH-069).

**Rationale:** Salience reranking captures signals (recency, lifecycle) that are
orthogonal to abstract relevance. The tier sort guarantees active documents
surface first; within each tier, abstract-boosted scores and recency still
differentiate.

### TEST-SAGE-BH-112: document_ids filter constrains keyword search to specified documents

**Decision:** When `filters.document_ids` is provided in keyword mode, only
chunks belonging to those documents are searched. This applies as both a
pre-filter (at the content store level, restricting the BM25 candidate set)
and a post-filter (in `_passes_scope`, as a safety net). Without pre-filtering,
rare terms in large documents are ranked off the candidate list by BM25 scores
from smaller, term-dense documents.

**Precondition:** Vault with three active documents. Document A contains the
target term once across many chunks. Documents B and C contain the target term
many times (higher BM25 scores). All three have completed pipelines.

**Input:** `discover(mode: keyword, query: <target_term>, filters: {document_ids: [A]})`

**Expected:**
- Results contain only Document A.
- Documents B and C are excluded despite having higher BM25 scores.

**Rationale:** The `document_ids` filter is an explicit user constraint. It must
restrict the search space, not merely post-filter an already-truncated result
set. Pre-filtering at the content store ensures the target document's chunks
compete only against each other for ranking.

### TEST-SAGE-BH-113: document_ids filter constrains semantic search to specified documents

**Decision:** Same constraint as BH-112, applied to semantic mode (both pure
vector and hybrid RRF). The `document_ids` filter is passed as a content-store
pre-filter and also enforced in `_passes_scope`.

**Precondition:** Same vault as BH-112.

**Input:** `discover(mode: semantic, query: <target_term>, filters: {document_ids: [A]})`

**Expected:**
- Results contain only Document A.
- Documents B and C are excluded.

**Rationale:** Semantic search has the same ranking dilution problem as keyword
search. Pre-filtering ensures the target document's chunks are not displaced
by closer matches from unrelated documents.

### TEST-SAGE-BH-114: document_ids filter works with scope ALL (post-filter)

**Decision:** The `document_ids` filter in `_passes_scope` applies to all scopes,
not just SPECIFIC. Previously, `document_ids` was only checked when
`scope == SPECIFIC`, causing the filter to be silently ignored for the default
`scope == ALL`.

**Precondition:** Two active documents with completed pipelines and shared
content terms.

**Input:** `discover(mode: keyword, query: <shared_term>, scope: all, filters: {document_ids: [A]})`

**Expected:**
- Only Document A appears in results.
- Document B is excluded by the post-filter even though it matches the query.

**Rationale:** Users pass `document_ids` to constrain results regardless of scope.
The scope controls lifecycle/authority gating; `document_ids` is an orthogonal
content constraint that must apply independently.

### TEST-SAGE-BH-115: document_ids filter with multiple IDs returns all matching documents

**Decision:** When `document_ids` contains multiple IDs, all matching documents
are returned. The content-store pre-filter uses an `IN` clause; the post-filter
checks set membership.

**Precondition:** Three active documents. All contain the query term.

**Input:** `discover(mode: keyword, query: <shared_term>, filters: {document_ids: [A, B]})`

**Expected:**
- Results contain Documents A and B.
- Document C is excluded.

**Rationale:** Multi-document filtering is the common case for cross-document
comparison queries (e.g., "search for MLPAO in PV07 and PV13").

---

## Agentic Round-Trip: Fetch and Supersede

These tests cover the agentic read-modify-reingest pattern. An agent retrieves
the original source file bytes from the vault (`get_document` with
`include_content=true`), edits the file locally, then ingests the modified file
as a new version that supersedes its predecessor (`ingest` with
`supersedes_document_id`). The vault's internal copy at
`storage_root/source_path` is the authoritative file; the agent's temporary
path is irrelevant after ingestion.

### TEST-SAGE-BH-116: get_document without include_content omits file content

**Artifact:** `sage/sage_core_api.openapi.yaml` (get_document endpoint)
**Category:** document_access
**Decision:** File content is opt-in. Default response shape is unchanged.

**Precondition:** Document A ingested from a small markdown file.

**Input:** `get_document(document_id: A)` with no `include_content` parameter
(or `include_content=false`).

**Expected:**
- Response contains the full Document record (id, title, metadata, lifecycle).
- Response does not contain `content` or `content_size` fields.

**Rationale:** Callers that only need metadata (lifecycle polling, tracker
dashboards) must not pay the transport cost of shipping file bytes. Backward
compatibility with existing callers is preserved.

### TEST-SAGE-BH-117: get_document with include_content returns base64 bytes and size

**Artifact:** `sage/sage_core_api.openapi.yaml` (get_document endpoint)
**Category:** document_access
**Decision:** `include_content=true` adds `content` (base64-encoded bytes) and
`content_size` (byte count) to the response. Bytes are read from the vault's
internal copy at `storage_root/source_path`.

**Precondition:** Document A ingested from a file with known bytes.

**Input:** `get_document(document_id: A, include_content: true)`

**Expected:**
- Response contains `content` field (base64 string) that decodes to the exact
  bytes of the file at `storage_root/source_path`.
- Response contains `content_size` field equal to the decoded byte length.
- Response still contains the full Document record.

**Rationale:** Agents need authoritative file bytes to edit, not a projection
reconstructed from chunks. The vault's internal copy is canonical; the original
ingest path is not retained and not needed.

### TEST-SAGE-BH-118: get_document with include_content rejects files above size ceiling

**Artifact:** `sage/sage_core_api.openapi.yaml` (get_document endpoint)
**Category:** document_access
**Decision:** Returning very large files as base64 in a JSON response bloats
transport and memory. A configurable size ceiling (default 100 MB) bounds
response size. Requests for files above the ceiling return 413 (Payload Too
Large) with the actual size in the error detail.

**Precondition:** Document A ingested; its file size exceeds the configured
`max_inline_content_bytes` ceiling.

**Input:** `get_document(document_id: A, include_content: true)`

**Expected:**
- Response is an error with status 413.
- Error detail names the actual size and the configured ceiling.
- The document metadata-only response (without `include_content`) still
  succeeds for the same document.

**Rationale:** Agents working with very large files should use a
filesystem-based flow outside this endpoint; the MCP/JSON path is optimized
for the common case of small-to-moderate documents.

### TEST-SAGE-BH-119: get_document with include_content returns 404 when vault file missing

**Artifact:** `sage/sage_core_api.openapi.yaml` (get_document endpoint)
**Category:** document_access
**Decision:** When a document record exists but the file at
`storage_root/source_path` is absent (manual deletion, disk corruption),
`include_content=true` must fail loudly rather than silently returning empty
bytes.

**Precondition:** Document A ingested, then the file at
`storage_root/source_path` is removed out-of-band.

**Input:** `get_document(document_id: A, include_content: true)`

**Expected:**
- Response is an error with status 404.
- Error detail names the missing file path.
- `get_document(document_id: A)` without `include_content` still returns the
  record (the graph record is intact).

**Rationale:** Silent empty bytes would let agents believe they are editing a
valid file. A loud failure surfaces the underlying vault inconsistency.

### TEST-SAGE-BH-120: ingest with supersedes_document_id links new version and archives predecessor

**Artifact:** `sage/sage_core_api.openapi.yaml` (ingest endpoint)
**Category:** ingestion
**Decision:** When `supersedes_document_id` is provided and ingestion succeeds,
SAGE atomically applies the `supersede` lifecycle transition on the
predecessor: creates a `supersedes` edge from new to old, sets the
predecessor's `lifecycle_status` to `archived`.

**Precondition:** Document A active, pipeline terminal.

**Input:** Ingest modified file as Document B with
`supersedes_document_id: A`.

**Expected:**
- Response is 201 with Document B, `lifecycle_status=active`.
- `get_document(A)` shows `lifecycle_status=archived`.
- A `supersedes` edge exists with `source_id=B`, `target_id=A`.
- `chain(document_id: A, edge_type: supersedes)` returns [A, B] in version
  order.

**Rationale:** The agentic round-trip produces a linked version history with
one call rather than requiring separate `ingest` + `set_lifecycle` calls. The
supersedes chain is the audit trail.

### TEST-SAGE-BH-121: ingest with supersedes_document_id rejects nonexistent predecessor

**Artifact:** `sage/sage_core_api.openapi.yaml` (ingest endpoint)
**Category:** ingestion
**Decision:** Validation of `supersedes_document_id` happens before projection.
A bogus predecessor ID must not result in a new document with no supersedes
link (which would be worse than no-op).

**Precondition:** Vault initialized. No document with id `nonexistent_id`.

**Input:** Ingest a new file with
`supersedes_document_id: "nonexistent_id"`.

**Expected:**
- Response is an error with status 404.
- No new document is created.
- No edge is created.

**Rationale:** Fail-fast on invalid supersede targets. Leaving an orphan new
version without the supersedes link would corrupt the version chain
invariants.

### TEST-SAGE-BH-122: ingest with supersedes_document_id rejects non-active predecessor

**Artifact:** `sage/sage_core_api.openapi.yaml` (ingest endpoint)
**Category:** ingestion
**Decision:** The `supersede` lifecycle transition is only valid from the
`active` state. If the predecessor is already `archived` or `completed`,
ingesting a new supersessor produces an inconsistent state and is rejected.

**Precondition:** Document A with `lifecycle_status=archived` (already
superseded, or archived via other path).

**Input:** Ingest a new file with `supersedes_document_id: A`.

**Expected:**
- Response is an error with status 409.
- Error detail names A's current lifecycle state and the required state
  (`active`).
- No new document is created.
- No edge is created.

**Rationale:** Consistent with `set_lifecycle` semantics: the `supersede`
transition is invalid outside the `active` state. The agent must either
reactivate the predecessor or target the current head of the chain.

### TEST-SAGE-BH-123: ingest with supersedes_document_id rejects identical content

**Artifact:** `sage/sage_core_api.openapi.yaml` (ingest endpoint)
**Category:** ingestion
**Decision:** A "supersession" whose content hash equals the predecessor's is
a no-op edit and must be rejected. Creating a superseding record with
identical content would pollute the version chain with duplicates.

**Precondition:** Document A active. Caller attempts to ingest a file with
bytes identical to A's file.

**Input:** Ingest the identical-content file with
`supersedes_document_id: A`.

**Expected:**
- Response is an error with status 409.
- Error detail identifies the content-hash match against the predecessor.
- No new document is created.
- No edge is created.
- A's lifecycle and content remain unchanged.

**Rationale:** Distinct from the generic `DuplicateContentError` (any vault
document): this error targets the specific case of supersede-with-no-changes.
The agent should detect no change before ingesting, but SAGE must catch the
mistake.

### TEST-SAGE-BH-124: ingest validates predecessor before projection

**Artifact:** `sage/sage_core_api.openapi.yaml` (ingest endpoint)
**Category:** ingestion
**Decision:** Predecessor validation (exists + active) happens before the
adapter runs Stage 1 (projection). A bogus or non-active predecessor must
not cause wasted projection work, wasted content-store writes, or partial
graph state. The common failure modes (404 predecessor, 409 non-active) are
detected before any new document record is created.

**Precondition:** No document exists with id `bogus_predecessor`. Document B
exists with `lifecycle_status=archived`.

**Input (two cases):**
- Ingest a file with `supersedes_document_id: "bogus_predecessor"`.
- Ingest a file with `supersedes_document_id: B`.

**Expected (both cases):**
- Response is an error (404 for case 1, 409 for case 2).
- No new document record is created.
- No projection, embeddings, or abstract are generated.
- No edge is created.

**Rationale:** Fail-fast keeps costly pipeline work behind validation. Strict
ingest+supersede atomicity against concurrent predecessor state changes is
deferred: the SAGE architecture has no document-deletion primitive (documents
are never deleted per ADR), so rolling back a successfully-ingested document
after a supersede failure is not possible. Pre-validation plus a narrow race
window between validation and supersede is the accepted posture. Agents that
observe a post-ingest supersede failure can reconcile by calling
`set_lifecycle(supersede)` explicitly on the new document.

---

## Agentic Round-Trip: Write-to-Path Delivery

Base64-in-JSON delivery (`include_content=true`) bloats by 33% and consumes
the agent's tool-result context. For moderately-sized documents this hits
MCP transport ceilings (Claude Desktop rolls large tool results into
synthetic attachments that the agent must read back chunk-by-chunk). These
tests cover the `write_to_path` alternative: SAGE writes the vault-local
file bytes to a caller-specified filesystem path, returning only metadata
and a verification hash. The caller reads, edits, and re-ingests via the
filesystem rather than through the tool-result channel.

### TEST-SAGE-BH-125: get_document write_to_path writes file and returns metadata only

**Artifact:** `sage/sage_core_api.openapi.yaml` (get_document endpoint)
**Category:** document_access
**Decision:** When `write_to_path` is provided, SAGE writes the
authoritative file bytes from `storage_root/source_path` to the target
path. The response contains the Document record plus `written_to`,
`content_size`, and `content_hash` for verification. No `content` field
(no base64 bytes in the response).

**Precondition:** Document A ingested from a file with known bytes. The
caller-chosen target path (e.g. `/tmp/agent_workspace/A.md`) does not
exist; its parent directory exists and is writable.

**Input:** `get_document(document_id: A, write_to_path:
"/tmp/agent_workspace/A.md")`

**Expected:**
- Response contains the full Document record.
- Response contains `written_to` equal to the caller-supplied path.
- Response contains `content_size` equal to the byte count of the written
  file.
- Response contains `content_hash` (hex digest) matching the document's
  `source_content_hash`.
- Response does not contain a `content` field.
- The file at the target path exists and its bytes exactly match the
  vault-local file at `storage_root/source_path`.

**Rationale:** The agentic round-trip works without moving bytes through
the tool-result channel. Metadata is small and cheap; the caller verifies
delivery via hash and size.

### TEST-SAGE-BH-126: get_document write_to_path refuses existing target

**Artifact:** `sage/sage_core_api.openapi.yaml` (get_document endpoint)
**Category:** document_access
**Decision:** SAGE refuses to overwrite an existing file at the target
path. Accidental overwrite of agent working state is a silent-corruption
class failure; callers must supply a fresh target or delete the prior
file first.

**Precondition:** Document A ingested. A file already exists at the
caller-chosen target path (any contents, even zero bytes).

**Input:** `get_document(document_id: A, write_to_path: <existing path>)`

**Expected:**
- Response is an error with status 409.
- Error detail names the target path.
- The existing file at the target path is unchanged (bytes match its
  pre-call state).

**Rationale:** Explicit is better than implicit. Agents that want to
replace a file can delete it first; SAGE does not make that decision on
the caller's behalf.

### TEST-SAGE-BH-127: get_document write_to_path requires existing writable parent directory

**Artifact:** `sage/sage_core_api.openapi.yaml` (get_document endpoint)
**Category:** document_access
**Decision:** SAGE does not create parent directories for the target
path. Missing or unwritable parent directories return a clear error
before any file operation is attempted.

**Precondition:** Document A ingested.

**Input (two cases):**
- Target path whose parent directory does not exist.
- Target path whose parent directory exists but is read-only.

**Expected (both cases):**
- Response is an error with status 400.
- Error detail names the offending parent directory and the reason
  (missing vs. unwritable).
- No file is created or modified.

**Rationale:** Silent mkdir behavior hides caller mistakes and risks
creating directories in unexpected places. A loud 400 surfaces the
problem at the interface.

### TEST-SAGE-BH-128: get_document rejects both include_content and write_to_path

**Artifact:** `sage/sage_core_api.openapi.yaml` (get_document endpoint)
**Category:** document_access
**Decision:** The two content-delivery modes are mutually exclusive.
Supplying both in a single request is an interface error (the caller
cannot want bytes in the response AND bytes on disk in one call).

**Precondition:** Document A ingested.

**Input:** `get_document(document_id: A, include_content: true,
write_to_path: "/tmp/A.md")`

**Expected:**
- Response is an error with status 400.
- Error detail names the conflict.
- No file is written.

**Rationale:** Clear mutual exclusion keeps the response shape
predictable. A caller that wants both can make two calls.
