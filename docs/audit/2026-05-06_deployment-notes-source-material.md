# Deployment-Driven Constraints: Source Material for Deployment Model Rewrite

**Date:** 2026-05-06
**Status:** Source material, not finished documentation
**Origin:** Drafted as candidate ADRs (CAS-ADR-023, CAS-ADR-024, CAS-ADR-025) during the SAGE Architecture Reference v2.0 rewrite, then reclassified as deployment-environment constraints rather than architectural decisions.

The three sections below describe behaviors that are deployment-driven (responses to the specific resources of the local Phase 1 deployment) rather than architectural decisions. They should not live in the ADR store. They should be incorporated into the Deployment Model document during its rewrite. The format here is the ADR draft format (Context, Decision, Rationale, Consequences); the Deployment Model rewrite reformats this content as operational description.

---

## Sequential Ingestion Pipeline

### Context

SAGE's ingestion pipeline runs three stages: projection (source adapter), indexing (chunking and embedding), and abstraction (LLM-generated semantic abstract per CAS-ADR-011). Earlier implementations dispatched indexing and abstraction asynchronously after projection completed, returning from `ingest()` while these stages ran in the background.

The local-machine deployment has a fixed memory envelope: 64 GB unified memory on Apple Silicon, of which the embedding model, the abstraction model, the OS, the development environment, and any other running processes must collectively fit. The embedding model occupies ~2 GB resident; the abstraction model occupies ~16-20 GB once loaded; per-document working state (KV cache during generation, token batches during embedding) adds an additional several GB during active processing.

Bulk ingest of multi-document batches creates concurrent in-flight documents under an asynchronous pipeline. Each in-flight document holds embedding-model state and abstraction-model context simultaneously. The MPS allocator, which manages the unified memory backing the abstraction model, hits OOM under bulk-ingest workflows when the cumulative working set across in-flight documents exceeds the available envelope.

### Operational Choice

Phase 1 runs the ingestion pipeline sequentially: all three stages complete before `ingest()` returns to the caller. The function returns when the document reaches a terminal `pipeline_status` (`abstraction_complete`, `abstraction_skipped`, or `failed`). Bulk-ingest workflows (CAS Application's `app_batch_ingest`, the `app_scan_directory` + `app_batch_ingest` pair) run their per-file loop synchronously: each file's full pipeline completes before the next file's pipeline begins.

### Rationale

The 64 GB unified memory envelope on Apple Silicon is the binding constraint. Sequential semantics keep peak memory bounded by the largest single-document working set rather than scaling with batch concurrency. The cost is throughput: a 1000-document batch takes approximately 1000x the time of a single document's pipeline. This is acceptable because bulk ingest is an occasional operation, not a hot path.

Phase 2 and Phase 3 deployments with larger memory budgets may revisit asynchronous semantics. The choice is local-environment-specific.

### Consequences

- `IngestionService.ingest()` returns only after the document reaches a terminal `pipeline_status`.
- The `pipeline_status` field on the Document record tracks progress through stages.
- Bulk ingest workflows emit per-document progress events.
- The synchronous portion of `sage_ingest` may exceed the 60-second MCP transport ceiling on large documents. Mitigation: bulk-ingest workflows use the SSE-streamed `/app/ingest` endpoint, which is not subject to the per-call ceiling.

---

## Lazy Abstraction Model Loading

### Context

SAGE's abstraction stage uses Qwen3-30B-A3B-Instruct-2507-4bit via MLX on Apple Silicon, per CAS-ADR-011. The model occupies approximately 16-20 GB of unified memory once loaded. Eager initialization (loading the model at service startup) imposes this cost on every SAGE process at startup, regardless of whether the process invokes abstraction.

A SAGE process commonly runs multi-vault, with some vaults using abstraction and others not. Eager initialization charges the abstraction model's memory cost to the multi-vault process even when only some vaults need it. Processes that only serve retrieval against pre-existing abstracts, processes attached to abstraction-disabled vaults, and processes used for ad-hoc graph operations would all carry the model in memory under eager initialization.

### Operational Choice

The abstraction model loads lazily on first invocation of `AbstractionProvider.generate_abstract()`, not at service startup. `AbstractionProvider.__init__()` does not load the model. `AbstractionProvider.generate_abstract()` calls a private `_ensure_loaded()` method on entry that loads the model if not already loaded; subsequent invocations within the same process reuse the loaded model. The model is held for the process lifetime; it is not unloaded.

`_ensure_loaded()` is idempotent. Failure to load (model file missing, MLX unavailable, model corruption) raises `RuntimeError` and propagates to the caller, who treats the failure as an `abstraction_skipped` outcome on the per-document pipeline (graceful degradation per CAS-ADR-011).

### Rationale

Lazy loading addresses the eager-load memory cost without requiring per-process configuration or out-of-process abstraction infrastructure. The cost is the latency on the first abstraction call (a few seconds), which is acceptable because abstraction is a per-document one-time cost in the ingestion pipeline; no interactive operation depends on first-call abstraction latency.

The deterministic-greedy-decoding property required by TEST-SAGE-AD-029 is preserved.

### Consequences

- Process baseline memory drops by approximately 16-20 GB for processes that have not yet abstracted.
- The first abstraction call after process start pays a model-load latency of a few seconds; subsequent calls do not.
- Long-running processes that interleave abstraction and other workflows hold the loaded model for the process lifetime.
- Multi-vault processes share the abstraction model across vaults: the model is process-level, not vault-level.
- Test coverage at AD-095 through AD-097.

---

## Single-Process Phase 1 Topology

### Context

SAGE Phase 1 deployment runs on a single local machine with a single user. Multiple components need to coexist: the SAGE Core API (FastAPI, exposed at `/sage_vaults/*`); the CAS Application backend (FastAPI, at `/app/*`); the CAS Application's React SPA (static assets at the root path); the FastMCP SSE transport (mounted at `/mcp`).

These components share state. They operate against the same set of vaults. They use the same SAGE service instances per vault. They share the same database connections and (when abstraction is enabled) the same loaded abstraction model.

### Operational Choice

Phase 1 runs all four components in a single uvicorn process. They share `app.state.vault_registry` (a dictionary keyed by vault id) and the same SAGE service instances per vault. The CAS Application backend mounts at `/app/*`, the SAGE Core API at `/sage_vaults/*`, the FastMCP SSE transport at `/mcp`, the React SPA as static assets at the root.

### Rationale

The single-process topology is justified by the Phase 1 scale (single user, single machine, single Python runtime) and by the load-bearing memory characteristics of SAGE's components. The embedding model, the abstraction model, and the database connections each consume non-trivial memory and would have to be replicated across processes under multi-process designs. The 64 GB unified memory envelope on Apple Silicon does not accommodate multiple copies of the abstraction model.

The architectural separation between SAGE Core API and CAS Application API is preserved by code structure and by the Formal Substrate's two separate spec files (`sage_core_api.openapi.yaml` and `cas_app_api.openapi.yaml`). The single-process deployment co-locates architecturally distinct components; it does not collapse them.

### Consequences

- A single uvicorn process serves all Phase 1 components. One log, one PID, one process to start and stop.
- The MCP server, REST callers, and CAS Application backend share in-memory vault state, the abstraction model when loaded, and database connections.
- A process crash takes down all components. Restart latency may include a model load on first abstraction.
- Phase 2 deployment moves the CAS Application backend to its own process or its own machine, communicating with the SAGE Core API over HTTP. The migration is a deployment change rather than a code refactor; the architectural surface separation is already in place.
- The MCP SSE-mounted form is preferred over standalone-stdio because it avoids duplicating model loads and database connections across processes.

---

[End of source material. Incorporate into Deployment Model rewrite.]
# Sections removed from SAGE Architecture Reference v2.0

These sections were removed because they are deployment-specific rather than architectural. Content here is the raw plain-text capture; reformat for the Deployment Model rewrite.

---

## §3.2.1: Phase 1: Local (Single Machine)

Phase 1: Local (Single Machine) All components run on a single machine. LanceDB (embedded) backs the content store. SQLite backs the graph store. One or more vaults reside on the local file system. AI clients connect via MCP either over stdio or, more commonly in current operation, through a small mcp-remote bridge that translates MCP into Server-Sent Events against a local FastAPI server. In Phase 1, the SAGE Core API and the CAS Application backend run together in a single uvicorn process serving a single port. The two components share an in-memory vault registry and the same  SAGE service instances. The MCP transport is mounted on the same FastAPI application. This single-process topology is described in detail in Section 3.5.2; its forward-compatibility properties (Phase 2 separation onto distinct processes or machines) are preserved by structuring the code as if the components communicated over HTTP.

---

## §3.2.2: Phase 2: Hybrid (Cloud + Local)

Phase 2: Hybrid (Cloud + Local) Shared vaults move to cloud infrastructure. The Core API for shared vaults runs as a hosted service. Team members connect via MCP over SSE or streamable HTTP. Personal vaults can remain local. The CAS Application backend separates from the SAGE Core API at this point, and the boundary that has existed in code structure since Phase 1 (separate URL prefixes, no shared in-memory state at the API surface) becomes a network boundary.

---

## §3.2.3: Phase 3: Mature Multi-Tenant

Phase 3: Mature Multi-Tenant Server-grade databases replace embedded stores for the cloud instance. PostgreSQL replaces SQLite for the graph store; pgvector or Qdrant replaces LanceDB for the content store. The Core API is unchanged; only the storage adapter implementations differ.

---

## §3.3.4: Phase 1 Implementation

Phase 1 Implementation LanceDB (content store):  Embedded, on-disk columnar store with native vector search, BM25 full-text search, and hybrid fusion. Handles both embedding vectors and full-text indexing in a single engine, eliminating the need for a separate full-text search system. Vault-rewrite operations (re-indexing, large bulk-update sweeps) require explicit table.optimize() to merge fragments and reclaim space; without it, fragments accumulate and disk usage can grow by orders of magnitude. IngestionService.ingest() does not invoke compaction on the per-document write path; periodic compaction is an operational responsibility documented in the Deployment Model. SQLite (graph store):  Embedded relational database with recursive CTE support for graph traversal (used by traverse, chain, and the supersedes lineage walks performed by the resolver), JSON functions for flexible metadata (Tier 3), and ACID transactions for lifecycle state changes. The entire graph for a vault fits in a single file, supporting the vault-as-directory portability model.

---

## §3.3.5: Embedding Model

Embedding Model SAGE uses  nomic-embed-text v1.5  via sentence-transformers, producing 768-dimensional L2-normalized vectors. The model runs on CPU with max_seq_length=2048 and batch_size=8 to avoid contention with the abstraction model&#x2019;s MLX/MPS unified-memory footprint on Apple Silicon. These tuning parameters are not arbitrary: at the default max_seq_length=8192 the attention matrix grows quadratically and competes with the abstraction model&#x2019;s working set. The current values are the smallest configuration that preserves embedding quality on the project&#x2019;s representative document set without forcing model swap-out under bulk ingest.

---

## §3.4.3: Phase 1 Adapters

Phase 1 Adapters The current production adapter set: Markdown adapter.  Pass-through. Reads the .md file and stores its content as the projection. Heading hierarchy is parsed from Markdown heading markers. Word .docx adapter.  Extracts paragraph text with style-based heading levels via python-docx. Maps Word paragraph styles (Heading 1 through Heading 9) to heading hierarchy markers in the projection. Includes a numbering engine that computes rendered heading number prefixes (decimal, upperRoman, lowerRoman, upperLetter, lowerLetter) with counter reset on parent-level increment, table extraction as pipe-delimited text rows, and cross-reference field resolution via cached display values. Word .dotx template adapter.  Extension overload of the docx adapter. For .dotx files, emits a template_style_inventory (Tier 3 metadata) carrying structured numbering details for templates only. Adapter-contributed tags surface custom-only style tags and all-styles numbering tags. XLSX adapter.  Multisheet projections. Heading paths reflect sheet names and within-sheet groupings. Column headers are surfaced in content. Preview rows are configurable per vault. Content hash is computed from the raw bytes of the source file. PDF adapter (v0.1).  Native-text only via pdfplumber (text extraction) and pypdf (outline and Info-dictionary reading). Outline-or-flat heading strategy: PDFs with a bookmarks outline produce nested heading nodes via outline-entry page ranges (depth capped at 10, deeper entries dropped with text preserved in the nearest ancestor&#x2019;s content); PDFs without an outline produce a single flat level-1 heading covering the full extracted text. Title resolution priority chain: /Info /Title → first outline entry → first body line of 120 characters or fewer → filename stem. Scanned-PDF detection via total_chars==0 heuristic; image-only PDFs emit empty projection with a pdf:scanned adapter tag and route to abstraction_skipped. OCR is explicitly out of scope for v0.1; users OCR scanned PDFs externally and re-ingest.

---

## §3.5.2: Single-Process Topology

Single-Process Topology In Phase 1, a single uvicorn process serves four interfaces: The SAGE Core API at the URL prefix /sage_vaults/*. The CAS Application backend at the URL prefix /app/* (covering /app/scan and /app/ingest). The CAS Application&#x2019;s React SPA as static assets at the root path. The FastMCP SSE transport at /mcp. All four share the same FastAPI application object, the same app.state.vault_registry (a dictionary keyed by vault id), and the same SAGE service instances (one set of IngestionService, RetrievalService, GraphOpsService, etc., per vault). The vault registry pattern matches the topology that the MCP server used from its inception, which made the unification straightforward. Despite the shared in-memory state, the code is structured as if the components communicated over HTTP. The CAS Application backend never imports SAGE service classes directly; it goes through the SAGE Core API surface. The URL-prefix boundary (/app/* versus /sage_vaults/*) is enforced by tests; the OpenAPI specifications (sage_core_api.openapi.yaml and cas_app_api.openapi.yaml) are separate documents reflecting the architectural separation. This forward-compatibility choice means that Phase 2 separation, when it occurs, is a deployment change rather than a code change: the CAS Application backend can be moved to its own process or its own machine, communicating with the SAGE Core API over HTTP, without requiring the kind of refactor that would be needed if the components shared in-memory state through direct method calls. The single-process topology is a Phase 1 architectural choice rather than a degenerate accident of co-location. The forward-compatibility properties (URL-prefix boundary, separate OpenAPI specifications, no direct cross-component imports) are part of the architecture, not an artifact of how the components happen to deploy.

---

## §9: Technology Stack Summary

Technology Stack Summary Component Phase 1 (Local) Phase 2+ (Cloud) Embedding model nomic-embed-text v1.5 via sentence-transformers (768d, L2-normalized, CPU device, max_seq_length=2048, batch_size=8) Same, or upgrade to a larger model if retrieval quality demands it Content store LanceDB (embedded, on-disk; manual table.optimize() on vault-rewrite operations) LanceDB on attached storage, or pgvector / Qdrant Graph store SQLite with thread-local connection pool (bounded ThreadPoolExecutor, default 4 workers; WAL mode; foreign keys enabled; recursive CTE support) PostgreSQL Abstraction model Qwen3-30B-A3B-Instruct-2507-4bit via MLX on Apple Silicon (lazy load on first invocation; greedy decoding for deterministic output; context-window-aware input truncation) Same, or hosted-inference equivalent Core API Python (FastAPI) Same, containerized MCP transport SSE mounted at /mcp on the FastAPI server, accessed via mcp-remote bridge; standalone-stdio mode also supported SSE / streamable HTTP (remote) Application backend FastAPI mounted on the same uvicorn process at /app/* Separate uvicorn process Application frontend React SPA served as static assets by the FastAPI process Separate hosting (CDN or hosted SPA service) Source file storage Local file system; vault sources/ may symlink to cloud-synced directories Cloud object storage or mounted volume Source adapters Markdown (native), Word .docx, Word .dotx template, XLSX, PDF (native-text only) Add OCR for scanned PDFs, email, OneNote, Teams, others as needed Indexing On-demand only (intentional ingestion; no file watcher per CAS-ADR-018) Same, plus optional scheduled batch for team consistency The Phase 1 stack is the live deployment. The Phase 2+ column describes architectural intent for a deployment that scales beyond a single developer machine; specific implementations are deferred to the deployment-model document for each phase.

---

## §3.5.1 Trailing Paragraph (MCP transport choice)

The MCP transport choice deserves a note. SSE transport mounts FastMCP&#x2019;s SSE app on the FastAPI server at /mcp, and the AI client uses a small mcp-remote bridge (npx mcp-remote@latest) as a stdio-to-SSE adapter. The bridge process is lightweight; the heavyweight SAGE state (model weights, database connections, vault registry) lives once in the FastAPI process and is shared  across REST callers, MCP callers, and the CAS Application backend. A standalone-stdio mode is also supported for clients that cannot use the SSE bridge, but launching a separate Python process per MCP client duplicates the embedding model, vault services, and SQLite connections, so the SSE-mounted form is preferred whenever possible.

---
