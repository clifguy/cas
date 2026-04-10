# SAGE/MCP Cleanup Refactor Test Specification

Formal test specifications for the code cleanup pass identified during
review on 2026-04-10. Tests are organized by issue number from the review.

---

## TEST-CLN-001: EdgeResult type consistency in batch ingest summary

**Artifact:** `sage/mcp_server.py` (app_batch_ingest), `app/backend/router.py` (ingest_endpoint)
**Category:** type safety, API contract
**Decision:** `edges_created` and `edges_staged` in the summary response are always
`dict[str, int]`, never `int`. When no edge inference runs, they are `{}`.

**Precondition:** SAGE vault initialized with markdown adapter.

**Test A -- No edge inference:**
- Input: Call `app_batch_ingest` with `infer_edges=False` and one valid file.
- Expected: Summary JSON has `"edges_created": {}` and `"edges_staged": {}` (empty dicts, not `0`).

**Test B -- With edge inference, no edges produced:**
- Input: Call `app_batch_ingest` with `infer_edges=True` and one file whose metadata
  produces no edge plan.
- Expected: Summary JSON has `"edges_created": {}` and `"edges_staged": {}`.

**Test C -- With edge inference, edges produced:**
- Input: Call `app_batch_ingest` with two versioned files that trigger a supersedes edge.
- Expected: `"edges_created"` is `{"supersedes": 1}` (dict with edge type keys and int counts).

**Rationale:** Frontend (`Ingest.tsx`) calls `Object.keys()` on these fields. Sending `0`
causes a runtime TypeError.

---

## TEST-CLN-003: Module-level imports in MCP server

**Artifact:** `sage/mcp_server.py`
**Category:** code organization
**Decision:** Error classes and utility imports are at module level, not inside functions.

**Test:** Verify the module imports compile without circular import errors.
- `from sage.api.errors import DocumentNotFoundError, StagingEdgeNotFoundError, ...`
  is at the top of `sage/mcp_server.py`.
- The module can be imported: `import sage.mcp_server` succeeds without ImportError.

**Rationale:** Local imports obscure dependencies and make static analysis harder.

---

## TEST-CLN-006: Unused schema fields marked deprecated

**Artifact:** `sage/models/schemas.py` (DiscoverRequest, DiscoverResponse)
**Category:** API hygiene
**Decision:** `authority_document_id`, `cursor` (DiscoverRequest), and `cursor`
(DiscoverResponse) are retained but marked with `deprecated=True` in their Field
definitions, since they exist in the OpenAPI spec but are not yet implemented.

**Test:** Verify that:
- `DiscoverRequest.model_json_schema()` includes a `deprecated: true` annotation
  for `authority_document_id` and `cursor`.
- `DiscoverResponse.model_json_schema()` includes `deprecated: true` for `cursor`.

**Rationale:** Removing fields breaks the OpenAPI spec contract. Deprecation signals
intent to remove in a future version.

---

## TEST-CLN-008: Edge inference exception includes edge details

**Artifact:** `app/backend/edge_inference.py` (resolve_and_execute)
**Category:** observability
**Decision:** The `except Exception` block in resolve_and_execute (line 246) logs
the exception with source_id, target_id, and edge_type context.

**Test:** Given a PlannedEdge that will fail during execution (e.g., target_id
references a non-existent document), verify:
- `result.edges_dropped` is incremented.
- The exception is logged via `logger.exception()` with source_id, target_id,
  and edge_type in the message (existing behavior preserved).
- No unhandled exception propagates.

**Rationale:** The existing logging IS adequate (line 247-250 already logs all three
fields). This test verifies the behavior is preserved after refactoring. No code
change needed here beyond ensuring the test exists.

---

## TEST-CLN-009: Type annotation on _passes_scope

**Artifact:** `sage/services/retrieval.py`
**Category:** type safety
**Decision:** `_passes_scope(self, doc: Document, request: DiscoverRequest)` has
explicit type annotation on the `doc` parameter.

**Test:** Static type check (mypy or manual verification) that `_passes_scope`
signature includes `doc: Document`. This is a code-level verification, not a
runtime test.

---

## TEST-CLN-011: Graph traversal helper naming

**Artifact:** `sage/storage/graph_store.py` (_traverse_sync internals)
**Category:** readability
**Decision:** The nested helper `_edge_select(from_col, to_col, ...)` is renamed to
`_edge_select(match_col, follow_col, ...)` to clarify semantics: `match_col` is the
column we match against to find edges, `follow_col` is the column we follow to the
next node.

**Test:** Existing traversal tests pass unchanged:
- `test_graph_ops.py::test_bh_035_traverse_outbound`
- `test_graph_ops.py::test_bh_036_traverse_inbound`
- `test_graph_ops.py::test_bh_037_traverse_both_directions`

**Rationale:** Pure rename of internal variables; external behavior unchanged.

---

## TEST-CLN-012: Ingestion service returns IngestResult dataclass

**Artifact:** `sage/services/ingestion.py`
**Category:** API clarity
**Decision:** `ingest()` returns `IngestResult` (dataclass with `document: Document`
and `is_new: bool`) instead of `tuple[Document, int]`. Callers use `.document` and
`.is_new` instead of unpacking a tuple with a magic HTTP status code.

**Test A -- New document:**
- Input: Ingest a new file.
- Expected: `result.document` is a Document, `result.is_new` is True.

**Test B -- Force re-ingestion:**
- Input: Ingest an existing file with `force=True`.
- Expected: `result.is_new` is False.

**Test C -- All existing callers updated:**
- `sage/mcp_server.py` (sage_ingest, app_batch_ingest)
- `app/backend/router.py` (ingest_endpoint)
- `sage/api/routers/ingestion.py`
- All test files in `tests/sage/test_ingestion.py`

**Rationale:** A tuple with a magic HTTP status code leaks transport concerns into
the service layer. `is_new` is the semantic signal callers actually need.

---

## TEST-CLN-002/004/005: Shared batch ingest logic and MCP server split

**Artifact:** `sage/mcp_server.py`, `app/backend/router.py`
**Category:** code duplication, module size
**Decision:** Extract the three-phase batch ingest logic (edge plan, per-file ingest,
edge execution) into `app/backend/batch_ingest.py` as a shared service function.
Both MCP `app_batch_ingest` and the router's `ingest_endpoint` call this function.

The MCP server file is split:
- `sage/mcp_server.py` -- server setup, vault registry, entry point, and imports
  the tool modules.
- `sage/mcp_tools_core.py` -- SAGE Core API tools (sage_ingest through sage_refresh_views).
- `sage/mcp_tools_app.py` -- Application tools (sage_list_vaults through app_batch_ingest).

**Test A -- MCP tool registration:**
- All tools previously registered on `mcp` are still discoverable after the split.
- `sage_ingest`, `sage_get_document`, `app_batch_ingest`, etc. are callable.

**Test B -- Shared batch ingest function:**
- Input: Call `batch_ingest_files(vault_services, files, infer_edges=True)`.
- Expected: Returns a `BatchIngestResult` with `documents_created`, `edges_created`,
  `edges_staged`, `edges_dropped`, `errors` fields.
- The same function drives both MCP JSON and router SSE outputs.

**Test C -- No behavioral regression:**
- All tests in `tests/app/test_mcp_app_tools.py` pass unchanged.
- All tests in `tests/sage/test_mcp_server.py` pass unchanged.

**Rationale:** 128-line duplicate logic across two files is a maintenance hazard.

---

## TEST-CLN-007: Service method signature convention (DEFERRED)

**Category:** consistency
**Status:** DEFERRED to a future pass.

**Observation:** Some services take `vault_id` as a separate parameter (ingestion),
others embed it in the request object (graph_ops via LinkRequest), and others don't
need it (metadata, retrieval). A full standardization would touch every service
constructor, every caller, and all tests.

**Decision:** Document the current convention rather than change it:
- Services that need vault_id for identity generation receive it as a parameter.
- Services that only need their pre-configured graph_store get vault context
  through their constructor.

No code change in this pass.

---

## Summary of Items

| # | Action | Risk | Files Affected |
|---|--------|------|----------------|
| 1 | Fix EdgeResult init | LOW | mcp_server.py, router.py |
| 3 | Module-level imports | LOW | mcp_server.py, ingestion.py, metadata.py |
| 6 | Deprecate schema fields | LOW | schemas.py |
| 8 | Verify exception logging | NONE | edge_inference.py (no change needed) |
| 9 | Add type annotation | LOW | retrieval.py |
| 10 | (INVALID -- asyncio IS used) | -- | -- |
| 11 | Rename helper params | LOW | graph_store.py |
| 12 | IngestResult dataclass | MEDIUM | ingestion.py, all callers |
| 2/4/5 | Extract shared service, split MCP | HIGH | multiple new/modified files |
| 7 | (DEFERRED) | -- | -- |
