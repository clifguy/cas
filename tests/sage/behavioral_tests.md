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
- Document `lifecycle_status` is `superseded`
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
- doc_old transitions to `superseded`
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

### TEST-SAGE-BH-026: Pipeline stages run as asyncio background tasks

**Artifact:** SAGE ingestion pipeline, async execution model
**Category:** ingestion, execution
**Decision:** Stages 2-3 run as asyncio background tasks in the FastAPI process.

**Precondition:** SAGE vault initialized.

**Input:** Ingest a document and immediately verify the HTTP response.

**Expected:**
- Ingest returns 201 with `pipeline_status: "projection_complete"`
- The response is returned before indexing begins
- Subsequent `get_document` calls show pipeline_status progressing through
  `indexing_in_progress` -> `indexing_complete` -> `abstraction_in_progress` ->
  `abstraction_complete`

**Rationale:** Async pipeline keeps ingest latency low. Callers poll or subscribe
for completion.


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
**Decision:** use_hybrid: false (default) returns pure vector similarity scores.

**Precondition:** Vault with indexed documents.

**Input:** `discover(mode: "semantic", query: "test query", use_hybrid: false)`

**Expected:**
- `relevance_score` reflects vector distance/similarity
- No BM25 influence on ranking

**Rationale:** Default behavior is pure semantic search; hybrid is opt-in.

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

### TEST-SAGE-BH-035: check_preconditions -- superseded does not satisfy

**Artifact:** `sage/sage_core_api.openapi.yaml` (check_preconditions)
**Category:** graph, lifecycle_interaction
**Decision:** Superseded documents are stale; they don't satisfy dependencies.

**Precondition:** doc_function depends_on doc_dep. doc_dep is `superseded`.

**Input:** `check_preconditions(function_id: doc_function.id)`

**Expected:**
- `satisfied: false`
- Check `actual` field reports `superseded`

**Rationale:** A superseded document has been replaced. Callers should depend on
the replacement.

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

### TEST-SAGE-BH-037: Traversal deduplicates by document with edge_count

**Artifact:** `sage/sage_core_api.openapi.yaml` (traverse endpoint)
**Category:** graph, traversal
**Decision:** Deduplication by target document; most recent edge shown; edge_count
reports total edge count.

**Precondition:** doc_a has 3 `references` edges to doc_b (created at t1, t2, t3).

**Input:** `traverse(start_id: doc_a.id, edge_type: "references", direction: "outbound")`

**Expected:**
- doc_b appears once in results
- The edge shown is the one created at t3 (most recent)
- `edge_count: 3`

**Rationale:** Compact results with the signal that duplicates exist. Prevents
result explosion in dense graphs.


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
- doc_b: `doc_type: "patent"`, `lifecycle_status: "superseded"`
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
- Directory `{storage_root}/views/by_lifecycle/superseded/` exists with symlink to
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
