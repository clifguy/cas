# CAS Application MCP Tool Tests

Tier 2 behavioral tests for the nine new MCP tools added to the SAGE MCP
server. Seven tools wrap new SAGE Core API endpoints; two tools wrap
application backend operations (directory scan, batch ingest).

All tools are registered in `sage/mcp_server.py` alongside the existing 11
SAGE tools (20 total). Tests follow the same direct-call pattern used by the
existing MCP tests: tool functions called directly with a pre-initialized vault
registry, bypassing MCP transport.

Design decisions encoded here:
- Single MCP server for both SAGE and app tools (shared vault configs and
  service instances).
- `sage_*` prefix for SAGE API tools; `app_*` prefix for application backend
  tools.
- Batch ingest returns synchronously with the summary; progress is emitted
  via MCP progress notifications during execution.
- All tools return JSON strings (success or structured error), matching
  existing tool conventions.

---

## 1. list_vaults

### TEST-APP-MCP-001: list_vaults returns all registered vaults

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-001
**Category:** mcp_tool, sage_api

**Decision:** `list_vaults` takes no parameters (no vault_id -- it operates
across vaults). Returns a JSON array of vault summary objects. This is the only
SAGE MCP tool without a vault_id parameter.

**Precondition:** MCP server running with two vaults registered (test_vault,
example_vault).

**Input:** Call `list_vaults()`.

**Expected:**
- Returns valid JSON string
- Parsed result is an array of 2 objects
- Each object includes: `id`, `name`, `description`, `storage_root`
- Vault IDs match the registered vaults

**Rationale:** The vault selector in the CAS Application sidebar and any MCP
client need to discover available vaults without knowing their IDs in advance.

### TEST-APP-MCP-002: list_vaults with no vaults returns empty array

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-002
**Category:** mcp_tool, sage_api

**Decision:** When no vaults are registered, returns `"[]"` (JSON empty array),
not an error.

**Precondition:** MCP server running with empty vault registry.

**Input:** Call `list_vaults()`.

**Expected:**
- Returns `"[]"` (valid JSON empty array)

**Rationale:** No vaults is a valid startup state. Returning an empty array
lets callers display a "no vaults configured" message.

---

## 2. get_vault_stats

### TEST-APP-MCP-003: get_vault_stats returns statistics and health indicators

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-003, TEST-APP-BE-004
**Category:** mcp_tool, sage_api

**Decision:** `get_vault_stats(vault_id)` returns the full statistics object
including health indicator counts. Mirrors the HTTP stats endpoint response.

**Precondition:** Vault with documents, edges, and staging edges.

**Input:** Call `get_vault_stats("test_vault")`.

**Expected:**
- Returns valid JSON string
- Parsed result includes:
  - `total_documents` (integer)
  - `by_lifecycle_status`, `by_doc_type`, `by_source_type` (objects)
  - `total_edges`, `by_edge_type` (integer, object)
  - `staging_edge_count` (integer)
  - `content_store_size_bytes`, `graph_store_size_bytes` (integers)
  - `last_ingestion_at` (string or null)
  - `health` object with: `pending_metadata_count`, `pending_edge_count`,
    `deferred_abstract_count`, `failed_ingestion_count`

**Rationale:** MCP clients (e.g., Claude Desktop) need vault health at a glance
to decide which operations to suggest.

### TEST-APP-MCP-004: get_vault_stats for empty vault returns zero counts

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-005
**Category:** mcp_tool, sage_api

**Decision:** An empty vault returns zero for all counts and null for
last_ingestion_at.

**Precondition:** Vault with no documents or edges.

**Input:** Call `get_vault_stats("test_vault")`.

**Expected:**
- `total_documents`: 0
- `total_edges`: 0
- `last_ingestion_at`: null
- All breakdown objects are empty

**Rationale:** Same behavior as the HTTP endpoint. Zero counts are valid.

### TEST-APP-MCP-005: get_vault_stats for unknown vault returns error

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-006
**Category:** mcp_tool, error_handling

**Decision:** Unknown vault_id returns a structured error JSON, not an exception.
Matches existing MCP error convention.

**Precondition:** Vault "nonexistent" not registered.

**Input:** Call `get_vault_stats("nonexistent")`.

**Expected:**
- Returns valid JSON string
- Parsed result has `"error": "unknown_vault"` field
- Message lists available vaults

**Rationale:** MCP tools must never raise exceptions. Structured error responses
let the caller present a meaningful message.

---

## 3. verify_hash

### TEST-APP-MCP-006: verify_hash returns match results for known hashes

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-007
**Category:** mcp_tool, sage_api

**Decision:** `verify_hash(vault_id, hashes)` accepts a list of hash strings
and returns a JSON object mapping each hash to its match result.

**Precondition:** Vault with one document having `source_content_hash` = "sha256:0000000000000000000000000000000000000000000000000000000000000000".

**Input:** Call `verify_hash("test_vault", ["sha256:0000000000000000000000000000000000000000000000000000000000000000", "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"])`.

**Expected:**
- Returns valid JSON string
- Parsed result maps:
  - `"sha256:0000000000000000000000000000000000000000000000000000000000000000"`: `{ "exists": true, "document_id": "<doc-id>" }`
  - `"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"`: `{ "exists": false }`

**Note:** Both example hashes are well-formed 64-hex digests on purpose. Hashes
are normalized and validated at the request boundary, so a malformed stand-in
for "absent" (e.g. `"sha256:unknown"`) rejects the whole call with
`invalid_sha256` rather than reporting `exists: false` for that entry.

**Rationale:** MCP clients performing scan-like operations need the same hash
check capability as the application backend.

### TEST-APP-MCP-007: verify_hash with empty list returns empty object

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-008
**Category:** mcp_tool, sage_api

**Decision:** Empty input returns `"{}"` (JSON empty object).

**Precondition:** Vault initialized.

**Input:** Call `verify_hash("test_vault", [])`.

**Expected:**
- Returns `"{}"` (valid JSON empty object)

**Rationale:** Empty input is a valid edge case. No special handling needed.

---

## 4. list_staging_edges

### TEST-APP-MCP-008: list_staging_edges returns Tier 2 edges

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-010
**Category:** mcp_tool, sage_api

**Decision:** `list_staging_edges(vault_id)` returns all Tier 2 suggested
edges awaiting review. Each entry includes id, source_id, target_id, edge_type,
inference_evidence, confidence_tier, and created_at.

**Precondition:** Vault with staging edges.

**Input:** Call `list_staging_edges("test_vault")`.

**Expected:**
- Returns valid JSON string
- Parsed result is an array of staging edge objects
- Each object includes all required fields
- Only Tier 2 edge types present

**Rationale:** MCP clients need to list staging edges for review workflows,
identical to the Edge Review tab in the CAS Application.

### TEST-APP-MCP-009: list_staging_edges returns empty array when none exist

**Artifact:** `sage/mcp_server.py`
**Category:** mcp_tool, sage_api

**Decision:** When no staging edges exist, returns an empty array.

**Precondition:** Vault with no staging edges.

**Input:** Call `list_staging_edges("test_vault")`.

**Expected:**
- Returns `"[]"` (valid JSON empty array)

**Rationale:** Empty staging is the common case after a full review pass.

---

## 5. update_staging_edge

### TEST-APP-MCP-010: update_staging_edge(action="confirm") moves edge to production

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-011
**Category:** mcp_tool, sage_api

**Decision:** `update_staging_edge(vault_id, edge_id, action="confirm")`
moves the specified staging edge to the production edge table. Returns the newly
created production edge. (Consolidated from the pre-audit
`sage_confirm_staging_edge` / `sage_dismiss_staging_edge` pair per the SAGE MCP
Tool Surface steering doc v3 audit.)

**Precondition:** Staging edge with known ID exists.

**Input:** Call `update_staging_edge("test_vault", "<staging-edge-id>", "confirm")`.

**Expected:**
- Returns valid JSON string
- Parsed result is a production edge object with auto-generated ID
- Staging edge no longer appears in `list_staging_edges` results
- Production edge visible via `traverse`

**Rationale:** MCP clients can confirm edges during conversational review
workflows without switching to the web UI.

### TEST-APP-MCP-011: update_staging_edge(action="dismiss") deletes from staging

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-012
**Category:** mcp_tool, sage_api

**Decision:** `update_staging_edge(vault_id, edge_id, action="dismiss")`
deletes the staging edge. Returns a confirmation object (not the deleted edge).

**Precondition:** Staging edge with known ID exists.

**Input:** Call `update_staging_edge("test_vault", "<staging-edge-id>", "dismiss")`.

**Expected:**
- Returns valid JSON string with confirmation (e.g., `{ "dismissed": true }`)
- Staging edge no longer appears in `list_staging_edges` results
- No production edge created

**Rationale:** Symmetric with confirm. MCP clients can dismiss false-positive
suggestions conversationally.

### TEST-APP-MCP-012: update_staging_edge against non-existent edge returns error

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-013
**Category:** mcp_tool, error_handling

**Decision:** Updating (confirming or dismissing) a staging edge that does not
exist returns a structured error JSON with "not_found" code. Invalid `action`
outside `{"confirm", "dismiss"}` returns `invalid_action`.

**Precondition:** No staging edge with ID "gone-001".

**Input:** Call `update_staging_edge("test_vault", "gone-001", "confirm")`.

**Expected:**
- Returns valid JSON string
- Parsed result has `"error"` field (e.g., "not_found")
- No exception raised

**Rationale:** MCP error convention: always return JSON, never raise.

---

## 6. list_pending_metadata

### TEST-APP-MCP-013: list_pending_metadata returns documents awaiting confirmation

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-014
**Category:** mcp_tool, sage_api

**Decision:** `list_pending_metadata(vault_id)` returns documents with
unconfirmed metadata, including the document record and extracted fields with
source annotations.

**Precondition:** Vault with documents pending metadata confirmation.

**Input:** Call `list_pending_metadata("test_vault")`.

**Expected:**
- Returns valid JSON string
- Parsed result is an array of pending metadata objects
- Each object includes `document` and `extracted_fields`
- `extracted_fields` maps field names to `{ value, source }` objects

**Rationale:** MCP clients can review and confirm metadata conversationally,
complementing the web UI's Metadata Review tab.

### TEST-APP-MCP-014: list_pending_metadata returns empty array when none pending

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-015
**Category:** mcp_tool, sage_api

**Decision:** No pending metadata returns an empty array.

**Precondition:** Vault with all metadata confirmed.

**Input:** Call `list_pending_metadata("test_vault")`.

**Expected:**
- Returns `"[]"` (valid JSON empty array)

**Rationale:** Standard empty-result convention.

---

## 7. list_directory

### TEST-APP-MCP-015: list_directory returns file list with parsed metadata

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-018
**Category:** mcp_tool, app_backend

**Decision:** `list_directory(vault_id, directory, max_depth=None)` walks the
directory, matches files against vault adapters, hashes files, parses filenames
using the vault's metadata_extraction config, checks hashes against the vault,
and returns the scan results. The tool reuses the same scan logic as the
`POST /app/scan` HTTP endpoint.

**Precondition:** Vault initialized. Directory exists with mixed file types,
including files already ingested and files with no matching adapter.

**Input:** Call `list_directory("test_vault", "/path/to/directory")`.

**Expected:**
- Returns valid JSON string
- Parsed result is an object with `files` array and `warnings` array
- Each file object includes:
  - `file_path`: absolute path to the file
  - `file_hash`: `"sha256:..."` content hash
  - `source_modified_at`: ISO 8601 timestamp from st_mtime
  - `adapter`: detected adapter name (or null)
  - `parsed_metadata`: object with `title`, `date`, `project`, `codes`,
    `version`, `doc_type`
  - `sage_status`: one of "new", "modified", "unchanged", "no_adapter"

**Rationale:** MCP clients (e.g., Claude Desktop) can preview directory contents
with parsed filename metadata before triggering ingestion, matching the Ingest
view's scan preview step. Parsed metadata enables the client to show doc_type
and version information in the scan preview.

### TEST-APP-MCP-016: list_directory validates directory existence

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-017
**Category:** mcp_tool, app_backend, error_handling

**Decision:** Invalid directory path returns a structured error JSON, not an
exception.

**Precondition:** MCP server running.

**Input:** Call `list_directory("test_vault", "/nonexistent/path")`.

**Expected:**
- Returns valid JSON string
- Parsed result has `"error"` field (e.g., "invalid_directory")
- Message: "Directory not found or not readable"

**Rationale:** MCP error convention: structured JSON errors for all failure modes.

### TEST-APP-MCP-017: list_directory respects max_depth

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-019
**Category:** mcp_tool, app_backend

**Decision:** The optional `max_depth` parameter limits recursion. Default (None)
applies the server's default depth ceiling, matching the HTTP endpoint behavior;
a walk cut by that default is reported as truncated. Server-side file-count and
hashed-byte ceilings always apply, and any ceiling cut sets `truncated: true`
plus a warning.

**Precondition:** Directory with nested subdirectories.

**Input:** Call `list_directory("test_vault", "/path/to/dir", max_depth=0)`.

**Expected:**
- Returns only files in the immediate directory
- Files in subdirectories excluded

**Rationale:** Depth limiting prevents accidentally scanning large directory
trees during conversational workflows.

### TEST-APP-MCP-018: list_directory handles permission errors as warnings

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-021
**Category:** mcp_tool, app_backend

**Decision:** Unreadable files/subdirectories are reported in the `warnings`
array rather than causing the tool to return an error.

**Precondition:** Directory with one unreadable subdirectory.

**Input:** Call `list_directory("test_vault", "/path/to/dir")`.

**Expected:**
- Returns valid JSON with `files` array (readable files) and `warnings` array
- Warnings include path and reason for each unreadable item
- No error response

**Rationale:** Partial results are more useful than total failure. The caller
can decide how to present warnings.

---

## 8. bulk_ingest_document

### TEST-APP-MCP-019: bulk_ingest_document processes files and returns summary

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-022, TEST-APP-BE-024, TEST-APP-BE-032
**Category:** mcp_tool, app_backend

**Decision:** `bulk_ingest_document(vault_id, files)` accepts a list of file objects
(`{ file_path, adapter, parsed_metadata? }`) and processes them sequentially via
SAGE's single-document ingest with two-phase edge inference. Returns the summary
object when all files are done. The tool runs synchronously from the caller's
perspective. Each file's `parsed_metadata` (if provided) is passed to SAGE's
IngestRequest.metadata for single-call ingestion with metadata.

**Precondition:** Vault initialized. Two valid source files exist.

**Input:** Call `bulk_ingest_document("test_vault", [{"file_path": "/path/doc1.md", "source_type": "markdown", "parsed_metadata": {"title": "Doc1", "codes": ["PV06"], "version": "v1", "doc_type": "design_spec"}}, {"file_path": "/path/doc2.md", "source_type": "markdown"}])`.

**Expected:**
- Returns valid JSON string (blocks until all files processed)
- Parsed result is a summary object with:
  - `documents_created`: `{ "new": N, "new_version": M }`
  - `metadata_pending` (integer)
  - `edges_created` (object by edge type, Tier 1 edges)
  - `edges_staged` (object by edge type, Tier 2 edges)
  - `edges_dropped` (integer, edges involving failed ingestions)
  - `abstracts_generated` (integer)
  - `abstracts_deferred` (integer)
  - `error_count` (integer)
  - `errors` (array of `{ filename, message }` for any failures)

**Rationale:** The MCP tool returns the same summary shape as the SSE endpoint's
final event, including edge inference results. Synchronous return simplifies the
caller's logic since MCP tools are request/response.

### TEST-APP-MCP-020: bulk_ingest_document emits MCP progress notifications

**Artifact:** `sage/mcp_server.py`
**Category:** mcp_tool, app_backend

**Decision:** During execution, the tool emits MCP progress notifications for
each file. Each notification includes `progress` (0.0 to 1.0) and a `message`
string identifying the current file and pipeline stage. This is the MCP-native
equivalent of SSE streaming.

**Precondition:** Vault initialized. Three valid source files.

**Input:** Call `bulk_ingest_document("test_vault", [...3 files...])` and observe
progress notifications.

**Expected:**
- Progress notifications emitted during execution (before final return)
- Each notification has `progress` (float, 0.0-1.0) and descriptive `message`
- Progress values increase monotonically
- At least one notification per file

**Rationale:** MCP's progress notification mechanism provides real-time feedback
to the client without requiring a streaming response. Claude Desktop renders
these as progress indicators.

### TEST-APP-MCP-021: bulk_ingest_document continues after per-file error

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-027
**Category:** mcp_tool, app_backend

**Decision:** If SAGE's ingest fails for a specific file, the tool records the
error and continues with the next file. The batch does not abort.

**Precondition:** Batch of 3 files. File 2 will fail (e.g., missing file).

**Input:** Call `bulk_ingest_document("test_vault", [file1, bad_file, file3])`.

**Expected:**
- Returns summary with `error_count: 1`
- `errors` array contains one entry with filename and error message
- `documents_created.new` reflects the 2 successful files
- No exception raised

**Rationale:** Per-file error isolation maximizes throughput. One bad file
should not waste the work done on valid files.

### TEST-APP-MCP-022: bulk_ingest_document with empty file list returns error

**Artifact:** `sage/mcp_server.py`, TEST-APP-BE-028
**Category:** mcp_tool, app_backend, error_handling

**Decision:** Empty file list returns a structured error JSON.

**Precondition:** MCP server running.

**Input:** Call `bulk_ingest_document("test_vault", [])`.

**Expected:**
- Returns valid JSON string
- Parsed result has `"error"` field
- Message: "No files selected for ingestion"

**Rationale:** Empty input is a caller logic error. Returning an error rather
than a zero-count summary makes the issue explicit.

---

## 9. Cross-Cutting MCP Conventions

### TEST-APP-MCP-023: All new tools return valid JSON strings

**Artifact:** `sage/mcp_server.py`
**Category:** mcp_tool, convention

**Decision:** Every new tool returns a JSON string (via `_serialize()` or
`_error_response()`). No tool returns a plain string, list, or raises an
exception.

**Precondition:** MCP server running with vault registered.

**Input:** Call each of the 9 new tools with valid parameters.

**Expected:**
- Each tool returns a string that `json.loads()` can parse
- No exceptions propagated to the MCP transport layer

**Rationale:** Existing MCP convention established by the 11 original tools.
Consistent serialization enables uniform client-side parsing.

### TEST-APP-MCP-024: SAGE tools use vault_id; app tools use vault_id

**Artifact:** `sage/mcp_server.py`
**Category:** mcp_tool, convention

**Decision:** All new tools except `list_vaults` take `vault_id` as their
first parameter. The `sage_*` tools route through `_get_vault()` for service
lookup. The `app_*` tools also use `_get_vault()` because they need the vault's
adapter registry and service instances.

**Precondition:** MCP server running with vault registered.

**Input:** Call `list_directory` with an unknown vault_id.

**Expected:**
- Returns structured error JSON with "unknown_vault" error
- Message lists available vaults

**Rationale:** App tools depend on vault services (adapter matching for scan,
ingestion service for batch ingest). The `_get_vault()` pattern ensures
consistent vault validation across all tool types.

### TEST-APP-MCP-025: Tool naming follows prefix convention

**Artifact:** `sage/mcp_server.py`
**Category:** mcp_tool, convention

**Decision:** SAGE Core API tools use `sage_` prefix. Application backend tools
use `app_` prefix. Both are registered on the same FastMCP instance.

**Precondition:** MCP server initialized.

**Input:** Enumerate registered tools on the MCP server.

**Expected:**
- 20 total tools registered (11 existing `sage_*` + 7 new `sage_*` + 2 `app_*`)
- All tool names follow the prefix convention
- No naming collisions

**Rationale:** The naming prefix maintains the logical boundary between SAGE
Core API operations and application-layer orchestration, even though they share
a process and vault registry.


---

## 10. search Catalog Mode

### TEST-APP-MCP-026: search catalog mode returns filtered documents

**Artifact:** `sage/sage_api_tools.py` (search)
**Category:** mcp_tool, retrieval

**Decision:** The search MCP tool supports catalog mode. When called with
`mode="catalog"` and filters, it returns a JSON response containing all matching
documents with document-level metadata only (no chunk content or relevance scores).

**Precondition:** Vault with 3 documents:
- doc_a: `doc_type="design_spec"`, `tags=["PV07"]`
- doc_b: `doc_type="glossary"`, `tags=["PV07"]`
- doc_c: `doc_type="design_spec"`, `tags=["PV08"]`

**Input:** `search(vault_id="test_vault", mode="catalog", scope="filtered", filters={"tags": ["PV07"]})`

**Expected:**
- Parsed response contains `mode: "catalog"`.
- `results` contains 2 entries (doc_a and doc_b).
- `total_available == 2`.
- Each result has `document` with id, title, doc_type, tags fields populated.
- Each result has `chunk_content: null`, `relevance_score: null`.

**Rationale:** MCP agents need deterministic document enumeration by metadata.
Catalog mode via MCP eliminates the need for multiple overlapping semantic
searches plus Python deduplication.


### TEST-APP-MCP-027: search catalog mode pagination with offset

**Artifact:** `sage/sage_api_tools.py` (search)
**Category:** mcp_tool, retrieval

**Decision:** The search MCP tool accepts an `offset` parameter for
catalog mode pagination. Combined with `limit`, this enables MCP agents to
page through large result sets.

**Precondition:** Vault with 5 documents (all non-failed).

**Input:**
- `search(vault_id="test_vault", mode="catalog", limit=2, offset=0)`
- `search(vault_id="test_vault", mode="catalog", limit=2, offset=2)`

**Expected:**
- First call: 2 results, `total_available == 5`.
- Second call: 2 results, `total_available == 5`.
- No overlap between result document IDs.

**Rationale:** Pagination is essential for MCP agents operating on vaults with
hundreds of documents. Without offset support, agents would need to retrieve
all documents in a single call, which could exceed response size limits.


---

## 11. chain

### TEST-APP-MCP-028: chain returns ordered version history

**Artifact:** `sage/sage_api_tools.py` (chain)
**Category:** mcp_tool, graph, chain

**Decision:** The `chain` MCP tool walks an edge chain to both ends from
any starting document and returns an ordered list with positional metadata.
Same semantics as the Core API chain endpoint (BH-089).

**Precondition:** Vault with 5 documents forming a linear supersedes chain:
v1 <- v2 <- v3 <- v4 <- v5.

**Input:** `chain(vault_id="test_vault", document_id=v3.id, edge_type="supersedes")`

**Expected:**
- JSON response with `chain` array of 5 entries ordered tail-to-head.
- `head_id` = v5.id, `tail_id` = v1.id.
- `query_position` = 2.
- `length` = 5.
- `is_linear` = true.
- Each chain entry has `id`, `title`, `version_label`, `lifecycle_status`, `document_date`.

**Rationale:** MCP agents working with versioned EXAMPLE documents (50+ versions common)
need an efficient single-call operation to retrieve the full version history.
The ordered list with positional metadata enables agents to reason about version
lineage without manual graph traversal and post-processing.


### TEST-APP-MCP-029: chain with non-existent document returns error

**Artifact:** `sage/sage_api_tools.py` (chain)
**Category:** mcp_tool, graph, chain, error

**Decision:** Standard MCP error response for invalid starting document,
consistent with other sage_* tools.

**Input:** `chain(vault_id="test_vault", document_id="nonexistent", edge_type="supersedes")`

**Expected:**
- JSON response with `error` key.
- Error message contains "document_not_found" or equivalent.

**Rationale:** Consistent error handling across all SAGE MCP tools. Callers
should not need to handle different error shapes per tool.
