# BatchIngestService Tests

Tier 2 behavioral tests for the shared BatchIngestService extracted from the
duplicate batch ingest logic in `sage/mcp_server.py` (JSON) and
`app/backend/router.py` (SSE). The service owns the three-phase pipeline;
callers provide delivery glue (JSON serialization or SSE streaming).

Tests are grouped by pipeline phase, then integration behavior.

---

## 1. Service Interface

### TEST-BIS-001: BatchIngestService accepts file descriptors and returns IngestSummary

**Artifact:** Code review finding (duplicate batch ingest logic)
**Category:** batch_ingest_service

**Decision:** BatchIngestService exposes a single async method
`run(files, vault_services, infer_edges, on_file_start, on_file_done, on_file_error)`
that returns an `IngestSummary` dataclass. The summary contains the same fields
both callers need: documents_created (new, new_version), metadata_pending,
edges_created, edges_staged, edges_dropped, abstracts_generated,
abstracts_deferred, error_count, errors.

**Precondition:** Service instantiated with a valid vault's SAGEServices.

**Input:** Two files with metadata, infer_edges=False.

**Expected:**
- Returns IngestSummary
- documents_created.new == 2
- error_count == 0
- edges_created == {} (inference disabled)

**Rationale:** A single return type ensures both MCP and router callers get
identical summary data without assembling it independently.

### TEST-BIS-002: Empty file list raises ValueError

**Artifact:** Existing behavior in both callers
**Category:** batch_ingest_service

**Decision:** The service raises ValueError for an empty file list rather than
returning a zero-count summary. Callers translate this to their error format
(JSON error for MCP, HTTP 400 for router).

**Precondition:** Service instantiated.

**Input:** `run(files=[], ...)`

**Expected:**
- ValueError raised with message "No files selected for ingestion"

**Rationale:** Empty list is a caller-level validation error. Raising lets each
caller handle it in their own idiom.

---

## 2. File Descriptor Normalization

### TEST-BIS-003: Service accepts dict-style file descriptors (MCP path)

**Artifact:** MCP server uses list[dict] from tool parameters
**Category:** batch_ingest_service

**Decision:** The service accepts file descriptors as a list of `FileDescriptor`
dataclasses. The MCP caller constructs these from raw dicts; the router caller
constructs them from Pydantic models. The service does not know about either
source format.

**Precondition:** Service instantiated.

**Input:** FileDescriptor with file_path, source_type, and parsed_metadata fields.

**Expected:**
- File ingested successfully
- Metadata applied correctly (title, date, project, codes, version, doc_type)

**Rationale:** A neutral input type decouples the service from both the MCP dict
convention and the router's Pydantic models.

### TEST-BIS-004: Service handles files without parsed_metadata

**Artifact:** Both callers allow null/missing parsed_metadata
**Category:** batch_ingest_service

**Decision:** When parsed_metadata is None, the service ingests the file with
no metadata dict. No default metadata is synthesized by the service.

**Precondition:** File descriptor with parsed_metadata=None.

**Input:** Single file, no metadata.

**Expected:**
- File ingested with metadata=None in IngestRequest
- IngestSummary reflects the ingestion

**Rationale:** Metadata is optional. The service should not invent defaults
that the caller did not provide.

---

## 3. Phase 1: Edge Plan Construction

### TEST-BIS-005: Edge plan built from scan items and existing vault docs

**Artifact:** Shared Phase 1 logic from both callers
**Category:** batch_ingest_service

**Decision:** When infer_edges=True, the service builds InferenceItems from the
file descriptors' parsed_metadata, fetches existing vault documents via
graph_store.list_all_documents(), and calls EdgeInferenceEngine.build_edge_plan().

**Precondition:** Vault with one existing document. Two new files forming a
version chain with it.

**Input:** Two files with version metadata, infer_edges=True.

**Expected:**
- EdgeInferenceEngine.build_edge_plan() called with scan_items (2) +
  existing_items (1)
- Resulting edge plan contains supersedes edges

**Rationale:** The edge plan requires visibility into both new files and existing
vault state. This is the duplicated logic being consolidated.

### TEST-BIS-006: Edge plan skipped when infer_edges=False

**Artifact:** Both callers check infer_edges flag
**Category:** batch_ingest_service

**Decision:** When infer_edges=False, no edge plan is built, no existing docs
are queried, and no edges are created post-ingest.

**Precondition:** Service instantiated.

**Input:** Files with version chain metadata, infer_edges=False.

**Expected:**
- graph_store.list_all_documents() not called
- IngestSummary: edges_created == {}, edges_staged == {}, edges_dropped == 0

**Rationale:** The flag provides a fast path for ingestion without graph
analysis, matching existing behavior in both callers.

### TEST-BIS-007: Existing docs mapped to InferenceItems with correct field mapping

**Artifact:** Both callers map doc.tags to codes, doc.version_label to version
**Category:** batch_ingest_service

**Decision:** Existing vault documents are mapped to InferenceItems with:
ref=doc.id, is_existing=True, parsed.title=doc.title, parsed.project=doc.project,
parsed.codes=doc.tags, parsed.version=doc.version_label, parsed.doc_type=doc.doc_type.

**Precondition:** Vault with documents that have tags and version_label set.

**Input:** infer_edges=True.

**Expected:**
- Existing items passed to build_edge_plan with correct field mapping
- Tags appear as codes, version_label appears as version

**Rationale:** Field name mismatch between Document schema (tags, version_label)
and ParsedMetadata (codes, version) must be handled consistently. Both callers
currently do this identically.

---

## 4. Phase 2: Per-File Ingestion

### TEST-BIS-008: Per-file ingestion calls SAGE ingestion service sequentially

**Artifact:** Both callers iterate files and call ingestion_service.ingest()
**Category:** batch_ingest_service

**Decision:** Files are ingested one at a time in order. Each call uses
IngestRequest with source=file_path, source_type=SourceType(source_type), and the
converted metadata dict.

**Precondition:** Three files queued.

**Input:** Three FileDescriptors.

**Expected:**
- ingestion_service.ingest() called three times in order
- Each call uses the correct file_path and source_type
- path_to_id mapping populated for all three

**Rationale:** Sequential ingestion matches existing behavior and allows
per-file progress reporting.

### TEST-BIS-009: Metadata dict conversion: codes joined, version mapped to version_label

**Artifact:** Both callers convert parsed_metadata to flat dict
**Category:** batch_ingest_service

**Decision:** The metadata dict conversion joins codes as comma-separated string,
maps version to version_label key, and passes title, date, project, doc_type
as-is.

**Precondition:** File with parsed_metadata containing codes=["PV06", "CF-1"]
and version="v7".

**Input:** Single file with full metadata.

**Expected:**
- IngestRequest.metadata["codes"] == "PV06,CF-1"
- IngestRequest.metadata["version_label"] == "v7"
- IngestRequest.metadata["title"], ["date"], ["project"], ["doc_type"] set

**Rationale:** SAGE metadata values are strings. The codes list must be
serialized. The version/version_label key mismatch must be handled by the
service, not duplicated in callers.

### TEST-BIS-010: Per-file errors do not abort the batch

**Artifact:** TEST-APP-BE-027
**Category:** batch_ingest_service

**Decision:** If ingestion_service.ingest() raises for one file, the service
catches the exception, records the error, and continues with the next file.

**Precondition:** Batch of 3 files, file 2 raises an exception.

**Input:** Three files, mock ingestion_service raises on file 2.

**Expected:**
- Files 1 and 3 ingested successfully
- IngestSummary.error_count == 1
- IngestSummary.errors contains entry for file 2
- path_to_id contains entries for files 1 and 3 only

**Rationale:** Per-file error isolation is a critical behavior. Consolidating
it in the service ensures both callers get identical error handling.

### TEST-BIS-011: Abstract tracking uses vault config abstraction.enabled

**Artifact:** Both callers check services.config.abstraction.enabled
**Category:** batch_ingest_service

**Decision:** For each successfully ingested file, if
config.abstraction.enabled is True, increment abstracts_generated; otherwise
increment abstracts_deferred.

**Precondition:** Vault with abstraction.enabled=False.

**Input:** Two files ingested successfully.

**Expected:**
- IngestSummary.abstracts_generated == 0
- IngestSummary.abstracts_deferred == 2

**Rationale:** Abstract tracking is config-driven. Both callers currently
implement this identically.

---

## 5. Phase 3: Post-Ingest Edge Execution

### TEST-BIS-012: Edge plan resolved and executed after all files ingested

**Artifact:** Both callers call resolve_and_execute after ingestion loop
**Category:** batch_ingest_service

**Decision:** After all files are processed, if an edge plan exists, the service
calls resolve_and_execute() with the edge plan, path_to_id mapping, graph_store,
graph_ops_service, and the vault's lifecycle transition table. The table is the
one the lifecycle service already built, not a second table built from the same
config, so the supersede transition cannot diverge between the batch path and
the explicit lifecycle path.

**Precondition:** Two versioned files forming a supersedes chain.

**Input:** Two files, infer_edges=True.

**Expected:**
- resolve_and_execute called with correct arguments
- IngestSummary.edges_created reflects Tier 1 edges
- IngestSummary.edges_staged reflects Tier 2 edges

**Rationale:** Edge execution depends on path_to_id populated during Phase 2.
Must run after all files are ingested.

### TEST-BIS-013: Edges dropped when referenced file failed ingestion

**Artifact:** resolve_and_execute drops edges for unresolved refs
**Category:** batch_ingest_service

**Decision:** If a file in the edge plan failed ingestion (not in path_to_id),
resolve_and_execute drops edges referencing that file.

**Precondition:** Edge plan references file A (succeeds) and file B (fails).

**Input:** Two files forming a version chain, file B raises during ingestion.

**Expected:**
- IngestSummary.edges_dropped >= 1
- No edge created between A and B

**Rationale:** Edges require valid document IDs on both ends. Failed ingestions
leave dangling refs that must be detected and dropped.

---

## 6. Progress Callbacks

### TEST-BIS-014: on_file_start callback invoked before each file ingestion

**Artifact:** Router needs SSE "started" events per file
**Category:** batch_ingest_service

**Decision:** The service accepts an optional async callback
`on_file_start(index, total, filename)` called before each
ingestion_service.ingest() call.

**Precondition:** Callback provided.

**Input:** Two files.

**Expected:**
- Callback called twice with (0, 2, "file1.docx") and (1, 2, "file2.md")
- Callbacks called before the corresponding ingest calls

**Rationale:** The router needs to emit SSE "started" events. The MCP caller
can pass None or a no-op. Callbacks decouple delivery format from pipeline logic.

### TEST-BIS-015: on_file_done callback invoked after successful ingestion

**Artifact:** Router needs SSE "completed" events with document_id
**Category:** batch_ingest_service

**Decision:** The service accepts an optional async callback
`on_file_done(index, total, filename, document_id)` called after successful
ingestion.

**Precondition:** Callback provided, ingestion succeeds.

**Input:** Two files, both succeed.

**Expected:**
- Callback called twice with correct document_ids
- Callbacks called after the corresponding ingest calls

**Rationale:** The router needs document_id in completed events. Passing it
through the callback avoids the router needing to track ingestion results.

### TEST-BIS-016: on_file_error callback invoked on per-file failure

**Artifact:** Router needs SSE "failed" events with error message
**Category:** batch_ingest_service

**Decision:** The service accepts an optional async callback
`on_file_error(index, total, filename, error_message)` called when
ingestion raises an exception.

**Precondition:** Callback provided, file 2 fails.

**Input:** Three files, file 2 raises.

**Expected:**
- on_file_error called once with error message from the exception
- on_file_done called for files 1 and 3
- Batch continues after error callback

**Rationale:** Error callbacks let the router emit "failed" SSE events without
knowing about exception handling internals.

### TEST-BIS-017: Callbacks are optional (None accepted)

**Artifact:** MCP caller does not need progress events
**Category:** batch_ingest_service

**Decision:** All three callbacks default to None. When None, no callback
is invoked and no error occurs.

**Precondition:** No callbacks provided.

**Input:** Two files, one fails.

**Expected:**
- Ingestion proceeds normally
- No callback-related errors
- IngestSummary populated correctly

**Rationale:** The MCP caller returns a single JSON summary and does not need
per-file events. Optional callbacks avoid forcing a no-op implementation.

---

## 7. Caller Integration

### TEST-BIS-018: MCP bulk_ingest_document delegates to BatchIngestService

**Artifact:** Code review finding (duplicate logic elimination)
**Category:** integration

**Decision:** After refactoring, bulk_ingest_document in mcp_server.py constructs
FileDescriptors from its dict inputs, calls BatchIngestService.run(), and
serializes the IngestSummary to JSON.

**Precondition:** MCP server running.

**Input:** bulk_ingest_document tool call with 2 files and infer_edges=True.

**Expected:**
- Returns JSON with same structure as before refactoring
- documents_created, edges_created, edges_staged, etc. all present
- Behavior identical to pre-refactoring implementation

**Rationale:** The refactoring must be behavior-preserving. The MCP tool
becomes a thin adapter from dict -> FileDescriptor -> service -> JSON.

### TEST-BIS-019: Router ingest_endpoint delegates to BatchIngestService

**Artifact:** Code review finding (duplicate logic elimination)
**Category:** integration

**Decision:** After refactoring, ingest_endpoint in router.py constructs
FileDescriptors from its Pydantic models, provides SSE-emitting callbacks,
calls BatchIngestService.run(), and emits the summary SSE event from the
returned IngestSummary.

**Precondition:** Application backend running.

**Input:** POST /app/ingest with 2 files.

**Expected:**
- SSE stream with per-file progress events (from callbacks)
- Final summary event with same fields as before refactoring
- Behavior identical to pre-refactoring implementation

**Rationale:** The refactoring must be behavior-preserving. The router
becomes a thin adapter from Pydantic -> FileDescriptor -> service -> SSE.

### TEST-BIS-020: metadata_pending counts unconfirmed documents, not new+version

**Artifact:** Bug fix (post-ingest summary in CAS app)
**Category:** batch_ingest_service

**Decision:** `IngestSummary.metadata_pending` reflects the count of ingested
documents whose `metadata_confirmed` flag is False after the per-file pipeline
completes, not a blanket `docs_new + docs_version`. The previous behavior
overstated the review queue: in vaults with `metadata_extraction.review_required`
false, ingest auto-confirms metadata (CAS-ADR-015 / ME-008), so the Review tab
would show zero items while the Ingest results panel reported "N pending."
The summary counter is incremented per file from the IngestResult's document
state.

**Precondition:** Service instantiated.

**Input:** Three files. Mock `ingestion_service.ingest` to return:
- file 1: IngestResult with document.metadata_confirmed=False (pending review)
- file 2: IngestResult with document.metadata_confirmed=True (auto-confirmed)
- file 3: IngestResult with document.metadata_confirmed=False (pending review)

**Expected:**
- `summary.metadata_pending == 2`
- `summary.docs_new == 3` (unchanged)
- `to_dict()["metadata_pending"] == 2`

**Rationale:** The summary counter must agree with what the Review tab will
show. Otherwise users see contradictory information across screens (the
regression that motivated this fix).

### TEST-BIS-021: Batch-inferred supersession syncs predecessor chunk lifecycle

**Artifact:** Bug fix (batch supersede skipped the chunk-store lifecycle sync)
**Category:** batch_ingest_service

**Decision:** When Phase 3 edge execution applies the Tier-1 `supersedes`
lifecycle side effect to a predecessor document, it also syncs the new
`lifecycle_status` to the predecessor's chunks via
`ContentStore.update_chunk_metadata`, exactly as `LifecycleService` and the
atomic ingest path do after their document writes. Pre-filter pushdown
requires the chunk-level `lifecycle_status` column to stay aligned with the
document's current state; without the sync, chunks of superseded documents
remain visible to active-filtered search indefinitely. The sync is
best-effort: a failure is recorded in `edge_warnings` under
`chunk_lifecycle_sync_failed` and never fails the batch.

**Precondition:** Services wired with a real in-memory content store. An
active predecessor (v1) exists in the graph with chunks indexed as
`lifecycle_status="active"`; an active-filtered chunk search returns them
(positive control).

**Input:** One v2 file of the same chain, `infer_edges=True`, so version-chain
inference plans the supersede.

**Expected:**
- Predecessor document lands in the transition table's landing state
  (`archived` under the base lifecycle).
- Every predecessor chunk carries `lifecycle_status="archived"`.
- An active-filtered chunk search no longer returns the predecessor's chunks.
- `edge_warnings` is empty.

**Rationale:** The document store and the content store must agree on
lifecycle state after every supersede surface, not just the explicit
lifecycle path — otherwise superseded content keeps surfacing in
lifecycle-filtered retrieval until the next reprojection.
