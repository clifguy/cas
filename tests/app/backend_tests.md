# CAS Application Backend Tests

Tier 2 behavioral tests for the CAS Application backend endpoints and new SAGE
Core API endpoints required by the application layer. Each test encodes a design
decision made during application spec development.

Tests are grouped by endpoint in dependency order: new SAGE API endpoints first
(they are consumed by the application backend), then application backend
endpoints.

---

## 1. SAGE API: Vault Listing

### TEST-APP-BE-001: GET /sage_vaults returns list of configured vaults

**Artifact:** App Spec v0.4, Section 4 (vault selector)
**Category:** sage_api

**Decision:** A new `GET /sage_vaults` endpoint returns all vaults registered
with the running SAGE instance. Each entry includes the vault ID, name,
description, and storage_root. This endpoint has no vault_id path parameter
because it operates across vaults.

**Precondition:** SAGE instance running with two vaults (example_vault,
personal_notes).

**Input:** `GET /sage_vaults`

**Expected:**
- 200 response
- Body is an array of vault summary objects
- Each object includes: `id`, `name`, `description`, `storage_root`
- Array contains exactly 2 entries

**Rationale:** The vault selector in the sidebar needs to enumerate available
vaults. Without this endpoint the frontend would need to hardcode vault IDs.

### TEST-APP-BE-002: GET /sage_vaults returns empty array when no vaults configured

**Artifact:** App Spec v0.4, Section 4
**Category:** sage_api

**Decision:** An empty SAGE instance returns an empty array, not an error.

**Precondition:** SAGE instance running with no vaults.

**Input:** `GET /sage_vaults`

**Expected:**
- 200 response
- Body is an empty array `[]`

**Rationale:** No vaults is a valid startup state (e.g., first-run setup).
Returning an empty array lets the frontend display a "no vaults configured"
message rather than an error.

---

## 2. SAGE API: Vault Statistics

### TEST-APP-BE-003: GET /sage_vaults/{vault_id}/stats returns vault statistics

**Artifact:** App Spec v0.4, Section 5.2 (Vault Statistics)
**Category:** sage_api

**Decision:** A new `GET /sage_vaults/{vault_id}/stats` endpoint returns all
ten Dashboard statistics in a single response. Statistics are computed on demand,
not cached.

**Precondition:** Vault with documents, edges, and staging edges.

**Input:** `GET /sage_vaults/example_vault/stats`

**Expected:**
- 200 response
- Body includes:
  - `total_documents` (integer)
  - `by_lifecycle_status` (object: status -> count)
  - `by_doc_type` (object: type -> count)
  - `by_source_type` (object: source_type -> count)
  - `total_edges` (integer)
  - `by_edge_type` (object: type -> count)
  - `staging_edge_count` (integer)
  - `content_store_size_bytes` (integer)
  - `sqlite_size_bytes` (integer)
  - `last_ingestion_at` (nullable ISO 8601 timestamp)

**Rationale:** A dedicated stats endpoint avoids the frontend having to make
multiple API calls and compute aggregates client-side. On-demand computation
ensures statistics are always current.

### TEST-APP-BE-004: Stats endpoint includes health indicator counts

**Artifact:** App Spec v0.4, Section 5.3 (Health Indicators)
**Category:** sage_api

**Decision:** The stats response includes health indicator counts as a nested
object: pending_metadata_count, pending_edge_count, deferred_abstract_count,
and failed_ingestion_count.

**Precondition:** Vault with items in each health category.

**Input:** `GET /sage_vaults/example_vault/stats`

**Expected:**
- Body includes `health` object with:
  - `pending_metadata_count` (integer)
  - `pending_edge_count` (integer, equals staging_edge_count)
  - `deferred_abstract_count` (integer)
  - `failed_ingestion_count` (integer)

**Rationale:** Health indicators are a Dashboard concern. Including them in
the stats response avoids a second API call for health status.

### TEST-APP-BE-005: Stats for empty vault returns zero counts

**Artifact:** App Spec v0.4, Section 5.2
**Category:** sage_api

**Decision:** An empty vault returns zero for all counts and null for
last_ingestion_at.

**Precondition:** Vault with no documents or edges.

**Input:** `GET /sage_vaults/personal_notes/stats`

**Expected:**
- `total_documents`: 0
- `total_edges`: 0
- `staging_edge_count`: 0
- `last_ingestion_at`: null
- All breakdown objects are empty `{}`

**Rationale:** Zero counts are the correct representation for an empty vault.
Null timestamp distinguishes "never ingested" from "ingested long ago."

### TEST-APP-BE-006: Stats for non-existent vault returns 404

**Artifact:** SAGE API convention
**Category:** sage_api

**Decision:** Requesting stats for an unknown vault_id returns 404.

**Precondition:** SAGE instance running; vault "nonexistent" not registered.

**Input:** `GET /sage_vaults/nonexistent/stats`

**Expected:**
- 404 response
- Body includes error detail: `"Vault 'nonexistent' not found"`

**Rationale:** Consistent 404 semantics across all vault-scoped endpoints.

---

## 3. SAGE API: Hash Check

### TEST-APP-BE-007: POST /sage_vaults/{vault_id}/hash-check returns matches

**Artifact:** Project tracker, approved application backend decisions
**Category:** sage_api

**Decision:** A new `POST /sage_vaults/{vault_id}/hash-check` endpoint accepts
`{ "hashes": ["sha256:abc...", ...] }` and returns matches with document IDs.
This enables the scan preview's new/modified/unchanged status determination
without side effects.

**Precondition:** Vault with documents having known source_content_hash values.

**Input:**
```json
POST /sage_vaults/example_vault/hash-check
{
  "hashes": ["sha256:abc123def456", "sha256:unknown", "sha256:design456"]
}
```

**Expected:**
- 200 response
- Body is an object mapping hashes to results:
  - `"sha256:abc123def456"`: `{ "exists": true, "document_id": "doc-001" }`
  - `"sha256:unknown"`: `{ "exists": false }`
  - `"sha256:design456"`: `{ "exists": true, "document_id": "doc-003" }`

**Rationale:** Bulk hash check in a single request avoids N+1 queries during
directory scan. The endpoint is read-only (no side effects).

### TEST-APP-BE-008: Hash check with empty array returns empty result

**Artifact:** Project tracker
**Category:** sage_api

**Decision:** An empty input array returns an empty result object.

**Precondition:** SAGE vault initialized.

**Input:**
```json
POST /sage_vaults/example_vault/hash-check
{ "hashes": [] }
```

**Expected:**
- 200 response
- Body: `{}`

**Rationale:** Empty input is a valid edge case (e.g., empty directory scan).
Returning empty rather than erroring avoids special-casing in the caller.

### TEST-APP-BE-009: Hash check against non-existent vault returns 404

**Artifact:** SAGE API convention
**Category:** sage_api

**Decision:** Consistent 404 for unknown vault_id.

**Precondition:** SAGE instance running.

**Input:** `POST /sage_vaults/nonexistent/hash-check { "hashes": ["sha256:x"] }`

**Expected:** 404 response.

**Rationale:** Same 404 convention as all vault-scoped endpoints.

---

## 4. SAGE API: Staging Edges

### TEST-APP-BE-010: GET /sage_vaults/{vault_id}/staging-edges lists staging edges

**Artifact:** App Spec v0.4, Section 8.1 (Edge Review)
**Category:** sage_api

**Decision:** A new `GET /sage_vaults/{vault_id}/staging-edges` endpoint returns
all Tier 2 suggested edges awaiting review. Each entry includes id, source_id,
target_id, edge_type, inference_evidence, and confidence_tier.

**Precondition:** Vault with staging edges.

**Input:** `GET /sage_vaults/example_vault/staging-edges`

**Expected:**
- 200 response
- Body is an array of staging edge objects
- Each object includes: id, source_id, target_id, edge_type,
  inference_evidence, confidence_tier, created_at
- Only Tier 2 edges present (covers, derived_from, bundles_with)

**Rationale:** The Edge Review tab needs the full staging edge list to render
grouped display. A dedicated endpoint isolates staging from production edges.

### TEST-APP-BE-011: POST /sage_vaults/{vault_id}/staging-edges/{edge_id}/confirm

**Artifact:** App Spec v0.4, Section 8.2 (Confirmation Workflow)
**Category:** sage_api

**Decision:** Confirming a staging edge moves it to the production edge table.
The staging edge is deleted and a corresponding production edge is created.

**Precondition:** Staging edge exists.

**Input:** `POST /sage_vaults/example_vault/staging-edges/staging-001/confirm`

**Expected:**
- 200 response
- Staging edge staging-001 no longer in staging table
- New edge in production table with same source_id, target_id, edge_type
- Production edge has a new auto-generated edge ID

**Rationale:** Confirmation is a state transition from staging to production.
The production edge gets its own ID because it enters the authoritative edge
table as a new record.

### TEST-APP-BE-012: POST /sage_vaults/{vault_id}/staging-edges/{edge_id}/dismiss

**Artifact:** App Spec v0.4, Section 8.2 (Confirmation Workflow)
**Category:** sage_api

**Decision:** Dismissing a staging edge deletes it from the staging table. No
production edge is created.

**Precondition:** Staging edge exists.

**Input:** `POST /sage_vaults/example_vault/staging-edges/staging-002/dismiss`

**Expected:**
- 200 response
- Staging edge staging-002 deleted
- No new production edge created

**Rationale:** Dismissed edges are false positives. Clean deletion prevents
re-review.

### TEST-APP-BE-013: Confirm/dismiss non-existent staging edge returns 404

**Artifact:** SAGE API convention
**Category:** sage_api

**Decision:** Confirming or dismissing a staging edge that does not exist (or
was already confirmed/dismissed) returns 404.

**Precondition:** No staging edge with ID "gone-001".

**Input:** `POST /sage_vaults/example_vault/staging-edges/gone-001/confirm`

**Expected:** 404 response.

**Rationale:** Idempotent-ish: if it's already gone, the client should know.
404 rather than 200 because the resource was not found.

---

## 5. SAGE API: Pending Metadata

### TEST-APP-BE-014: GET /sage_vaults/{vault_id}/pending-metadata

**Artifact:** App Spec v0.4, Section 7.1 (Review Queue)
**Category:** sage_api

**Decision:** A new `GET /sage_vaults/{vault_id}/pending-metadata` endpoint
returns documents whose extracted metadata has not been confirmed. Each entry
includes the document record and the extracted fields with source annotations.

**Precondition:** Vault with documents pending metadata confirmation.

**Input:** `GET /sage_vaults/example_vault/pending-metadata`

**Expected:**
- 200 response
- Body is an array of pending metadata objects
- Each object includes:
  - `document`: full document record
  - `extracted_fields`: object mapping field names to
    `{ value, source, alt_value?, alt_source? }`
- Source values are one of: "filename", "content", "default"

**Rationale:** The Metadata Review tab needs both the document record and the
extraction details (with source annotations) to render the review queue and
support inline editing.

### TEST-APP-BE-015: Pending metadata returns empty array when none pending

**Artifact:** App Spec v0.4, Section 7
**Category:** sage_api

**Decision:** When no documents have pending metadata, return an empty array.

**Precondition:** Vault with all metadata confirmed.

**Input:** `GET /sage_vaults/example_vault/pending-metadata`

**Expected:**
- 200 response
- Body: `[]`

**Rationale:** Empty is a valid state. Dashboard health indicator renders "0".

---

## 6. SAGE API: Pipeline Status Filtering on Discover

### TEST-APP-BE-016: Discover endpoint accepts pipeline_status filter

**Artifact:** Project tracker (SAGE API gaps)
**Category:** sage_api

**Decision:** The existing `POST /sage_vaults/{vault_id}/discover` endpoint
accepts an optional `pipeline_status` filter in its scope/filters. This allows
the Dashboard to query for failed or deferred documents.

**Precondition:** Vault with documents in various pipeline states (complete,
failed, abstraction_skipped).

**Input:**
```json
POST /sage_vaults/example_vault/discover
{
  "mode": "deterministic",
  "scope": "filtered",
  "filters": { "pipeline_status": "failed" }
}
```

**Expected:**
- 200 response
- Results include only documents with pipeline_status "failed"
- Documents with other pipeline statuses excluded

**Rationale:** The Dashboard's "failed ingestions" health indicator needs to
retrieve failed documents. Pipeline status filtering on discover avoids a
dedicated endpoint.

---

## 7. Application Backend: Directory Scan

### TEST-APP-BE-017: POST /app/scan validates directory existence

**Artifact:** App Spec v0.4, Section 6.1 (Directory Input)
**Category:** app_backend

**Decision:** The scan endpoint validates that the provided path exists and is
a readable directory. Invalid paths return a 400 error with a descriptive
message.

**Precondition:** Application backend running.

**Input:**
```json
POST /app/scan
{ "vault_id": "example_vault", "directory": "/nonexistent/path" }
```

**Expected:**
- 400 response
- Body includes: `"detail": "Directory not found or not readable"`

**Rationale:** Server-side validation is required because the browser cannot
access the local filesystem. Fast failure prevents wasted work.

### TEST-APP-BE-018: POST /app/scan returns file list with status and parsed metadata

**Artifact:** App Spec v0.4, Section 6.2 (Scan Preview); project tracker (edge
inference design decisions, scan result shape)
**Category:** app_backend

**Decision:** The scan endpoint walks the directory recursively (unlimited depth
by default), matches files against vault adapters by extension, hashes each file,
checks hashes against SAGE (via hash-check endpoint), and parses filenames using
the vault's metadata_extraction config. Returns a list of files with status and
parsed metadata.

**Precondition:** Directory with files matching and not matching vault adapters.
Some files already ingested. Vault has metadata_extraction config with
known_code_patterns and keyword_to_doc_type.

**Input:**
```json
POST /app/scan
{
  "vault_id": "example_vault",
  "directory": "/path/to/example_inbox"
}
```

**Expected:**
- 200 response
- Body is an array of scan result objects
- Each object includes:
  - `file_path`: absolute path to the file
  - `file_hash`: `"sha256:..."` content hash
  - `source_modified_at`: ISO 8601 timestamp from st_mtime
  - `adapter`: detected adapter name (or null)
  - `parsed_metadata`: object containing:
    - `title`: string (always present)
    - `date`: string or null (YYYY-MM-DD)
    - `project`: string or null
    - `codes`: array of strings (may be empty)
    - `version`: string or null (raw v-prefixed string)
    - `doc_type`: string or null (resolved via keyword_to_doc_type then
      code_to_doc_type)
  - `sage_status`: one of "new", "modified", "unchanged", "no_adapter"
- Files with no adapter match: `adapter` is null, `sage_status` is "no_adapter",
  `parsed_metadata` still populated (parsing is independent of adapter detection)
- New files (hash not in vault): `sage_status` is "new"
- Modified files (path exists in vault but hash differs): `sage_status` is
  "modified"
- Unchanged files (hash matches stored): `sage_status` is "unchanged"

**Rationale:** The scan endpoint encapsulates the full scan workflow (walk,
match, hash, check, parse) in a single server-side operation. Parsed metadata
enables the edge inference engine to build an edge plan before ingestion begins.

### TEST-APP-BE-019: POST /app/scan respects optional depth limit

**Artifact:** App Spec v0.4, Section 6.2 (configurable depth limit)
**Category:** app_backend

**Decision:** An optional `max_depth` parameter limits recursion depth. Default
is unlimited (null). Depth 0 means the directory itself (no recursion). Depth 1
includes immediate children only.

**Precondition:** Directory with nested subdirectories.

**Input:**
```json
POST /app/scan
{
  "vault_id": "example_vault",
  "directory": "/path/to/example_inbox",
  "max_depth": 1
}
```

**Expected:**
- Only files in the immediate directory are returned
- Files in subdirectories are excluded

**Rationale:** Depth limiting prevents accidentally scanning an entire drive.
The default (unlimited) matches the spec's "unlimited depth by default."

### TEST-APP-BE-020: POST /app/scan computes content hash for each file

**Artifact:** Project tracker (hash-check integration)
**Category:** app_backend

**Decision:** Content hashes are computed using the same algorithm SAGE uses
(SHA-256 of file content, prefixed with "sha256:"). This ensures hash
comparisons against the vault are valid.

**Precondition:** Directory with files.

**Input:** `POST /app/scan { "vault_id": "example_vault", "directory": "..." }`

**Expected:**
- Hashes computed for all files with matching adapters
- Hash format matches SAGE's source_content_hash format ("sha256:...")
- Files without matching adapters do not need hashes (status is "no_adapter")

**Rationale:** Hash algorithm mismatch between scan and vault would make
every file appear as "new." Using the same algorithm guarantees correct
new/modified/unchanged classification.

### TEST-APP-BE-021: Scan handles permission errors gracefully

**Artifact:** App Spec v0.4, Section 6.1
**Category:** app_backend

**Decision:** Files or subdirectories that cannot be read (permission denied)
are reported as warnings in the response, not as fatal errors.

**Precondition:** Directory with one unreadable subdirectory.

**Input:** `POST /app/scan { "vault_id": "example_vault", "directory": "..." }`

**Expected:**
- 200 response (not 500)
- Readable files returned normally
- Response includes a `warnings` array with entries for unreadable paths
- Unreadable files/directories are skipped, not included in the file list

**Rationale:** A single unreadable subdirectory should not abort the entire scan.
Reporting warnings lets the user know what was skipped.

---

## 8. Application Backend: Batch Ingestion

### TEST-APP-BE-022: POST /app/ingest streams progress via SSE

**Artifact:** App Spec v0.4, Section 6.3 (Ingestion Progress); project tracker
(SSE streaming)
**Category:** app_backend

**Decision:** The ingest endpoint returns a Server-Sent Events (SSE) stream.
Each event reports progress for one file. The content type is
`text/event-stream`.

**Precondition:** Application backend running. Files selected for ingestion.

**Input:**
```json
POST /app/ingest
{
  "vault_id": "example_vault",
  "files": [
    { "path": "/path/to/example_inbox/doc1.docx", "source_type": "docx" },
    { "path": "/path/to/example_inbox/doc2.md", "source_type": "markdown" }
  ]
}
```

**Expected:**
- Response content type: `text/event-stream`
- Stream emits events as ingestion proceeds
- Events are valid SSE format (`data: {...}\n\n`)

**Rationale:** SSE provides real-time progress to the browser without polling.
The browser's EventSource API handles reconnection natively.

### TEST-APP-BE-023: SSE event format for per-file progress

**Artifact:** App Spec v0.4, Section 6.3
**Category:** app_backend

**Decision:** Each SSE event is a JSON object with fields: `event_type`,
`file_index`, `total_files`, `filename`, `stage`, and `status`. The `stage`
field is one of: "projection", "indexing", "abstraction". The `status` field
is one of: "started", "completed", "failed", "skipped".

**Precondition:** Ingestion in progress.

**Input:** Observe SSE events during ingestion.

**Expected:**
- Each file produces at least a "started" and a terminal event
  ("completed", "failed", or "skipped")
- The `stage` field tracks pipeline progress within a file
- The `file_index` (0-based) and `total_files` enable progress calculation
- Example event:
  ```
  data: {"event_type":"progress","file_index":0,"total_files":2,"filename":"doc1.docx","stage":"indexing","status":"completed"}
  ```

**Rationale:** Structured JSON events allow the frontend to render a progress
bar, per-file status, and stage indicator from a single event stream.

### TEST-APP-BE-024: SSE emits summary event on completion

**Artifact:** App Spec v0.4, Section 6.4 (Results Summary)
**Category:** app_backend

**Decision:** After all files are processed, the stream emits a final summary
event with `event_type: "summary"`. The summary includes: documents_created
(new, new_version), metadata_pending, edges_created_by_type,
edges_staged_by_type, abstracts_generated, abstracts_deferred, error_count.

**Precondition:** Ingestion completes (all files processed or cancelled).

**Input:** Observe final SSE event.

**Expected:**
- Final event has `event_type: "summary"`
- Summary fields present:
  - `documents_created`: `{ "new": N, "new_version": M }`
  - `metadata_pending`: integer
  - `edges_created`: `{ "supersedes": N, ... }` (Tier 1 auto-created by type)
  - `edges_staged`: `{ "covers": N, ... }` (Tier 2 by type)
  - `abstracts_generated`: integer
  - `abstracts_deferred`: integer
  - `error_count`: integer
- Stream closes after summary event

**Rationale:** The summary event provides all data needed for the Step 4
Results Summary without requiring a second API call. Stream closure signals
completion to the EventSource client.

### TEST-APP-BE-025: Ingestion calls SAGE single-document ingest with metadata per file

**Artifact:** Project tracker (application backend decisions); FS v1.2
(IngestRequest.metadata)
**Category:** app_backend

**Decision:** The application backend calls SAGE's existing single-document
ingest endpoint (`POST /sage_vaults/{vault_id}/documents`) for each selected
file in sequence. Each call includes the `metadata` dict populated from the
scan result's parsed_metadata (title, date, project, codes, version, doc_type).
Batch ingestion is application-layer orchestration, not a SAGE Core API operation.

**Precondition:** Two files selected for ingestion, each with parsed_metadata
from scan.

**Input:** `POST /app/ingest` with 2 files.

**Expected:**
- SAGE ingest endpoint called twice (once per file)
- Each call includes `adapter`, `source` (file path), and `metadata` dict
- metadata dict contains parsed filename segments:
  `{ "title": "...", "date": "...", "project": "...", "codes": "PV06,CF-1",
  "version": "v7", "doc_type": "design_spec" }`
- Results from each call contribute to the summary event

**Rationale:** The metadata dict enables single-call ingestion with filename-
derived metadata instead of a separate ingest + update_metadata sequence.
Codes are serialized as a comma-separated string in the metadata dict (SAGE
metadata values are strings).

### TEST-APP-BE-026: Cancel stops processing remaining files

**Artifact:** App Spec v0.4, Section 6.3 (cancel behavior)
**Category:** app_backend

**Decision:** When the client closes the SSE connection (browser cancel), the
server stops processing remaining files after the current file completes. Files
already ingested are retained.

**Precondition:** Ingestion of 5 files in progress. 2 files completed.

**Input:** Client closes the SSE connection after receiving progress for file 2.

**Expected:**
- Current file (file 3, if in progress) completes or aborts
- Files 4 and 5 are not processed
- Documents from files 1 and 2 remain in the vault
- No summary event emitted (connection closed)

**Rationale:** SSE connection closure is the natural cancellation signal. The
server detects the closed connection on the next write attempt. Completing
the current file avoids partial document state.

### TEST-APP-BE-027: Ingestion handles per-file SAGE errors gracefully

**Artifact:** App Spec v0.4, Section 6.3 (per-file status)
**Category:** app_backend

**Decision:** If SAGE's ingest endpoint returns an error for a specific file
(e.g., adapter failure, duplicate content), the application backend emits an
SSE event with `status: "failed"` and an `error` field, then continues with
the next file. The batch does not abort.

**Precondition:** Batch of 3 files where file 2 will fail (e.g., corrupt .docx).

**Input:** `POST /app/ingest` with 3 files.

**Expected:**
- File 1: progress events, completed
- File 2: progress event with `status: "failed"`, `error: "..."` message
- File 3: progress events, completed (not skipped due to file 2's failure)
- Summary event: error_count = 1

**Rationale:** One bad file should not abort the entire batch. Per-file error
isolation maximizes throughput for valid files.

### TEST-APP-BE-028: Ingest endpoint rejects empty file list

**Artifact:** Application backend validation
**Category:** app_backend

**Decision:** An empty file list returns 400 rather than opening an SSE stream
that immediately emits a summary with all-zero counts.

**Precondition:** Application backend running.

**Input:**
```json
POST /app/ingest
{ "vault_id": "example_vault", "files": [] }
```

**Expected:**
- 400 response
- Body: `"detail": "No files selected for ingestion"`

**Rationale:** An empty file list is a client-side logic error (the UI should
disable the button when nothing is selected). Returning 400 catches this early.

---

## 9. Application Backend: Edge Inference Integration

### TEST-APP-BE-031: Ingest endpoint accepts scan_results with parsed metadata

**Artifact:** Project tracker (edge inference design decisions)
**Category:** app_backend

**Decision:** The ingest endpoint accepts the full scan result array (including
parsed_metadata per file) rather than just file paths. This enables the two-phase
edge inference: pre-ingest analysis uses parsed_metadata to build an edge plan
before any SAGE ingest calls are made.

**Precondition:** Scan completed with parsed metadata.

**Input:**
```json
POST /app/ingest
{
  "vault_id": "example_vault",
  "files": [
    {
      "file_path": "/path/to/example_inbox/2026-03-09_EXAMPLE_PV06_Sample-Set_v6.docx",
      "source_type": "docx",
      "parsed_metadata": {
        "title": "Claim-Set", "date": "2026-03-09", "project": "EXAMPLE",
        "codes": ["PV06"], "version": "v6", "doc_type": "design_spec"
      }
    },
    {
      "file_path": "/path/to/example_inbox/2026-03-09_EXAMPLE_PV06_Sample-Set_v7.docx",
      "source_type": "docx",
      "parsed_metadata": {
        "title": "Claim-Set", "date": "2026-03-09", "project": "EXAMPLE",
        "codes": ["PV06"], "version": "v7", "doc_type": "design_spec"
      }
    }
  ]
}
```

**Expected:**
- Ingest proceeds normally with per-file SAGE calls
- Edge inference runs after all files are ingested
- Summary event includes edge creation counts

**Rationale:** Passing parsed_metadata through the ingest request avoids
re-parsing filenames. The scan result is the single source of parsed metadata.

### TEST-APP-BE-032: Ingest runs two-phase edge inference

**Artifact:** Project tracker (edge inference design decisions)
**Category:** app_backend

**Decision:** Batch ingestion executes two-phase edge inference: (1) pre-ingest
analysis builds an edge plan from parsed metadata + existing vault documents;
(2) post-ingest creation resolves file paths to document IDs and executes edges.
SSE events report edge creation progress after file ingestion completes.

**Precondition:** Batch with version chain and code match opportunities.

**Input:** Batch with v6, v7 of Claim-Set and a PV06 Checklist.

**Expected:**
- Pre-ingest: edge plan computed (supersedes + covers edges planned)
- File ingestion: 3 SAGE ingest calls with per-file SSE progress
- Post-ingest: edge plan executed
  - supersedes edges created via SAGE link()
  - covers edges inserted into staging table
- SSE events emitted for edge creation phase
- Summary event includes `edges_created` and `edges_staged` counts

**Rationale:** Two-phase inference ensures the edge plan is computed from the
full manifest context (all files visible) while edge execution uses real
document IDs from SAGE.

### TEST-APP-BE-033: Summary event includes edge counts by type

**Artifact:** Project tracker (edge inference design decisions)
**Category:** app_backend

**Decision:** The summary event's edge fields are broken down by edge type.
`edges_created` reports Tier 1 edges created in the production graph.
`edges_staged` reports Tier 2 edges inserted into the staging table.
`edges_dropped` reports edges that could not be created due to failed
ingestions.

**Precondition:** Batch with mixed Tier 1 and Tier 2 edge results.

**Input:** Batch producing 2 supersedes edges and 3 covers staging edges.

**Expected:**
Summary event includes:
- `edges_created`: `{ "supersedes": 2 }`
- `edges_staged`: `{ "covers": 3 }`
- `edges_dropped`: 0

**Rationale:** Type-level breakdown lets the Results Summary view show which
kinds of edges were inferred, matching the Dashboard's by_edge_type display.

---

## 10. Application Backend: Endpoint Conventions

### TEST-APP-BE-034: Application endpoints use /app/ prefix

**Artifact:** Project tracker (approved architectural decision)
**Category:** app_backend

**Decision:** Application backend endpoints mount on `/app/` prefix. SAGE Core
API endpoints mount on `/sage_vaults/`. Both are served by the same FastAPI
instance.

**Precondition:** Application running.

**Input:** `GET /app/scan` returns 405 (method not allowed, but route exists).

**Expected:**
- `/app/*` routes are handled by the application router
- `/sage_vaults/*` routes are handled by the SAGE Core API router
- No route collisions between the two prefixes

**Rationale:** Separate URL prefixes maintain the boundary between application
orchestration logic and SAGE's protocol-neutral Core API, supporting future
separation into distinct services.

### TEST-APP-BE-035: Application backend serves compiled React frontend

**Artifact:** Project tracker (approved architectural decision)
**Category:** app_backend

**Decision:** The application backend serves the compiled React build output
as static files. The frontend is accessible at the root URL (`/`).

**Precondition:** React frontend built (`npm run build`). Application started.

**Input:** `GET /`

**Expected:**
- Returns the React SPA's index.html
- Static assets (JS, CSS) served from the build output directory
- Client-side routing handled by the SPA (all non-API paths fall through to
  index.html)

**Rationale:** One uvicorn process, one port, everything local. No separate
frontend server needed at runtime.


### TEST-APP-BE-036: Pending metadata includes document_date in extracted_fields

**Artifact:** `sage/api/routers/pending_metadata.py` (_build_extracted_fields)
**Category:** sage_api

**Decision:** The pending metadata endpoint includes `document_date` in
the extracted_fields dict so the Metadata Review UI can display and edit it.
The source annotation reflects how the date was derived: `"filename"` when
the filename parser provided a date code, `"default"` when the value fell
back to source_modified_at.

**Precondition:** Vault with a document ingested from a file whose name
contains a date code (e.g., `2026-04-10_EXAMPLE_PV07_checklist_v1.md`).
Metadata not yet confirmed.

**Input:** `GET /sage_vaults/{vault_id}/pending-metadata`

**Expected:**
- Response includes the document in the pending list
- `extracted_fields` contains key `"document_date"`
- `extracted_fields["document_date"].value == "2026-04-10"`
- `extracted_fields["document_date"].source == "filename"`
- For a document without a filename date (fallback case),
  `source == "default"`

**Rationale:** document_date is a reviewable metadata field like title,
doc_type, and project. Users should see how the date was derived (filename
vs. filesystem fallback) and be able to correct it before confirmation.
