# SAGE Architecture Reference v2.0 — Section-by-Section Rewrite Plan

**Date:** 2026-05-06
**Status:** Draft for review before .docx authoring begins
**Predecessor:** `2026-03-30_CAS_REF_SAGE-Architecture_v1_4_2.docx` (tracker-recorded as v1.4)
**Successor target:** `2026-05-06_CAS_REF_SAGE-Architecture_v2_0.docx`

## Verification basis

This plan is grounded in:

- The full text of SAGE Architecture Reference v1.4.2 (extracted to `outputs/docx_extracts/`).
- The ten ADRs that touch SAGE: 011, 012, 014, 015, 016, 017, 018, 019, 020, 021 (full text extracted to `outputs/adr_extracts/`).
- Substrate manifest revisions v1.0 through v1.14.
- Project Tracker v91, with §10 Working Code as the as-built record.
- Code-level spot-checks of `sage/models/enums.py` (edge types, retrieval modes, pipeline statuses), `sage/models/edge_registry.py` (default policy assignments), and the layout of `sage/services/`, `sage/api/routers/`, `sage/storage/`, and `app/backend/`.

## Authoring standard

Per session decision 2026-05-06: the document must be self-contained narrative. A reader unfamiliar with the codebase should come away understanding what SAGE is, how it works, and why each substantive decision was made, without consulting code, commits, or the ADR store. ADRs continue to function as the audit trail and conflict-resolution authority; the document cites them by ID where their decisions are incorporated.

## Target structure for v2.0

The top-level section structure stays the same. Internal expansion happens within §3, §4, §5, and §7. One new appendix is added.

```
1  Introduction
   1.1 Design Principles
2  Conceptual Foundations
   2.1 From Associative Memory to Operational Knowledge Graph
   2.2 The Object-Storage Separation Pattern
   2.3 Multi-Source Ingestion and Indexable Projections
   2.4 Relationship to Existing Systems
3  System Architecture
   3.1 Architectural Layers
   3.2 Deployment Model
       3.2.1 Phase 1: Local (Single Machine)
       3.2.2 Phase 2: Hybrid (Cloud + Local)
       3.2.3 Phase 3: Mature Multi-Tenant
   3.3 Storage Architecture
       3.3.1 Storage Abstraction Layer
       3.3.2 Content Store
       3.3.3 Graph Store
       3.3.4 Phase 1 Implementation
       3.3.5 Embedding Model
       3.3.6 Chunking Strategy
       3.3.7 Indexing
   3.4 Source Adapter Architecture
       3.4.1 Ingestion Pipeline (sequential semantics, memory envelope)  [revised]
       3.4.2 Adapter Interface Contract
       3.4.3 Phase 1 Adapters (markdown, docx, xlsx, pdf, dotx)  [revised]
       3.4.4 Projection Persistence
       3.4.5 Source Change Detection (intentional re-ingest only)  [rewritten]
       3.4.6 Metadata Extraction (caller-owned with chain inheritance)  [rewritten]
       3.4.7 Edge Inference (mechanical-vs-curated provenance gate)  [rewritten]
       3.4.8 UI-Layer File Metadata Normalization  [new]
   3.5 Client Access Architecture
       3.5.1 Protocol Adapters and Transports  [revised]
       3.5.2 Single-Process Topology  [new]
4  Object Model
   4.1 Document
   4.2 User
   4.3 Editor Model (Access Control)
   4.4 Metadata Model
       4.4.1 Tier 1: Core (Invariant)
       4.4.2 Tier 2: Vault-Configured
       4.4.3 Tier 3: Source-Type-Specific
   4.5 Edge (Graph Relationship)  [revised — anchor fields, valid_until_version]
   4.6 Edge Type Taxonomy  [revised — 11 types including retracts and merged_from]
   4.7 Edge Type Registry and Resolution Model  [new]
   4.8 Vault
   4.9 Lifecycle State Machine  [revised — superseded state removed]
   4.10 Decision Logs  [new]
   4.11 Pipeline Status  [new]
5  Retrieval Architecture
   5.1 Retrieval Modes  [revised — semantic, keyword, deterministic, catalog]
   5.2 Source Scope
   5.3 Combined Retrieval Parameters
   5.4 Hybrid Search and Reranking
   5.5 Salience Reranking  [new]
   5.6 Abstract-Boosted Retrieval  [new]
   5.7 Document-Level Response Mode  [new]
   5.8 Pre-Filter Resolution  [new]
   5.9 Chain Walk Operation  [new]
6  Access Control and Governance
   6.1 Design Principle
   6.2 User Registration
   6.3 Editor-Based Write Control
   6.4 No-Delete Invariant (documents only)  [clarified]
   6.5 Provenance Tracking
   6.6 Source Authority
   6.7 Blast Radius Assessment
7  Core API Operations  [substantial expansion]
   7.1 Operation Catalog by Tag  [new structure]
   7.2 Application API Surface  [new]
8  Multi-Vault Architecture
   8.1 Vault Isolation Model
   8.2 Cross-Vault Isolation
   8.3 Physical Layout  [revised paths]
9  Technology Stack Summary  [revised — production adapter realities]
10 Open Design Questions  [pruned and updated]
Appendix A  Agent Consumption Patterns
Appendix B  Revision History  [v2.0 entry added]
Appendix C  CAS ADR Index  [10 new entries]
Appendix D  Worked Example: Chain-Scoped Edge Resolution  [new]
```

## Per-section rewrite plan

Legend: **STAY** = no change; **REVISE** = update inline; **REPLACE** = rewrite from scratch; **NEW** = section that did not exist; **PRUNE** = remove. Each item cites the ADR it incorporates where applicable.

### §1 Introduction

**STAY** with light edit. Update the closing paragraph that points to "Phase 1 implementation details are documented in Section 3.3.4" to acknowledge that Phase 1 is now substantially implemented and operating at beta-level production. Two sentences.

### §1.1 Design Principles

**REVISE.** The six principles are sound and stay. Two adjustments:

- "Flexible deployment" stays but acknowledges that Phase 1 is now the live deployment.
- Add one principle: "Intentional ingestion. Documents enter SAGE only through explicit caller invocation; SAGE provides no file watcher, polling loop, or auto-ingest. The caller's intentional act is the auditable signal that something belongs in the canonical record." Cite ADR-018.

### §2 Conceptual Foundations

**STAY** with two minor updates:

- §2.3 Multi-Source Ingestion: add a sentence noting that the abstraction stage now serves an additional consumer beyond retrieval orientation: it is the document-level relevance-triage card that an autonomous agent reads after discovering the document via MCP, before deciding whether to fetch it. Cite ADR-020.
- §2.4 Relationship to Existing Systems: STAY. The comparison table remains accurate.

### §3 System Architecture

#### §3.1 Architectural Layers

**STAY.** Five layers remain accurate.

#### §3.2 Deployment Model

**REVISE.** Phase 1 description is updated to reflect the as-built state: a single uvicorn process serving both the SAGE Core API (`/sage_vaults/*`) and the CAS Application backend (`/app/*`), with the FastMCP SSE transport mounted at `/mcp`. Phases 2 and 3 stay as forward-looking. Cite the proposed new ADR on single-process topology (see ADR draft candidates below).

#### §3.3 Storage Architecture

**REVISE.** The §3.3.x subsection structure stays. Updates:

- §3.3.4 Phase 1 Implementation: LanceDB and SQLite descriptions stay. Add a sentence acknowledging the LanceDB compaction operational note: vault-rewrite operations require explicit `table.optimize()` to avoid fragment bloat; this is a known operational concern documented in the Deployment Model.
- §3.3.5 Embedding Model: add the production-adapter realities — nomic-embed-text-v1.5 via sentence-transformers, 768-dim L2-normalized output, embedding-on-CPU with `max_seq_length=2048` to avoid MPS/MLX unified-memory contention on Apple Silicon. This is a production-tuning consequence; cite a candidate ADR if you choose to capture it (see ADR draft candidates).
- §3.3.6 Chunking Strategy: STAY. Heading-hierarchy chunking is unchanged.
- §3.3.7 Indexing: REVISE the sentence about IVF-PQ indexing to note the empirical threshold and that doc_type is now stored on the chunks schema and applied as a pre-filter (not just metadata).

#### §3.4 Source Adapter Architecture

##### §3.4.1 Ingestion Pipeline

**REPLACE.** The current text describes the three-stage pipeline (projection, indexing, abstraction) at an architectural level; this stays. The rewrite adds:

1. **Sequential semantics.** All three stages complete before `ingest()` returns. Pipeline status flows projection_complete → indexing_in_progress → indexing_complete → abstraction_in_progress → abstraction_complete (or abstraction_skipped, or failed). The decision to make the pipeline sequential rather than fire-and-forget is a deliberate response to memory pressure: bulk ingest with concurrent in-flight documents exhausted the 64 GB unified memory envelope. Cite candidate ADR (Sequential pipeline).

2. **Lazy MLX model load.** The Qwen3-30B abstraction model loads on first `generate_abstract()` call rather than at service startup. Baseline RAM is ~20 GB lower for vaults that have not yet generated an abstract. Cite candidate ADR (Lazy MLX load).

3. **UI-layer normalization.** Files copied into the vault from outside `storage_root` are stripped of UI-invisibility markers (BSD UF_HIDDEN chflag, com.apple.FinderInfo invisible bit) on macOS to preserve the user-auditable-canonical-artifact contract. Cite ADR-016.

4. **Pipeline status as a first-class field on the document record.** Surfaces in get_document, discover, and traverse so callers can observe ingestion progress and failure.

##### §3.4.2 Adapter Interface Contract

**REVISE.** The contract still holds. Two additions:

- **Adapter-contributed tags.** Adapters may return tags namespaced by the adapter name (e.g., `pdf:scanned`, `pdf:truncated`, dotx style-surface tags). These merge into `document.tags` on ingest, with stale-tag stripping on force re-ingest via declared `adapter_tag_prefixes`.
- **`source_modified_at` capture.** Adapters extract source filesystem modification time when available; this populates a distinct `source_modified_at` field on the document record (separate from `created_at`, which is SAGE's ingestion timestamp).

##### §3.4.3 Phase 1 Adapters

**REPLACE.** Update the adapter list to reflect the production set:

- **Markdown** (native, source modification time captured)
- **Word .docx** (heading extraction via paragraph styles, numbering engine for rendered heading prefixes, cross-reference field resolution, table extraction)
- **Word .dotx template** (extension-overload of docx adapter; emits `template_style_inventory` with structured numbering details for templates only)
- **XLSX** (multisheet projections, column headers, preview rows, content-hash on raw bytes)
- **PDF** (PdfAdapter v0.1, native-text only via pdfplumber + pypdf; outline-or-flat heading strategy; scanned-PDF detection via `total_chars==0` heuristic with `pdf:scanned` tag; failure semantics for encrypted/corrupt PDFs)
- **Obsidian** (planned; Obsidian's plain-Markdown-on-disk model trivializes integration through the existing Markdown adapter; wikilink-based edge inference is a future addition)

OCR is explicitly out of scope for Phase 1.

##### §3.4.4 Projection Persistence

**STAY.** Projections stored on the document record; no intermediate files; export_projection writes to disk on demand.

##### §3.4.5 Source Change Detection

**REPLACE wholesale.** The current section describes a file watcher as Phase 1 capability #2. ADR-018 (2026-04-25) formally excludes file watching, polling, and auto-ingest from SAGE's surface. The rewrite states:

> SAGE indexes but does not own source files. Sources have a life independent of SAGE. Change detection is tied to caller-initiated re-ingest: when a caller invokes `sage_ingest`, `app_scan_directory`, or `app_batch_ingest` against a known path, SAGE re-runs the source adapter, compares the new content_hash against the stored `source_content_hash`, and creates a `supersedes`-linked successor document if they differ. There is no file watcher, no polling loop, no scheduled scan. The caller's intentional act is the auditable signal that something belongs in the canonical record.

Three capabilities remain (provenance capture, hash-comparison change detection on re-ingest, versioned re-ingestion). Cite ADR-018.

##### §3.4.6 Metadata Extraction

**REPLACE wholesale.** Two ADRs reshape this section:

- **ADR-015 (2026-04-16)** moved FilenameParser from `app/backend/` to `sage/services/`. Metadata extraction is a SAGE-level capability that runs uniformly across all ingestion entry points.
- **ADR-021 (2026-05-01)** establishes that metadata authority belongs to the caller. `IngestRequest.needs_review: bool` (default false) controls whether SAGE invokes filename inference. With `needs_review=false`, SAGE commits caller-supplied values as-is. With `needs_review=true`, SAGE may invoke filename inference for fields the caller did not supply, and marks the document `metadata_confirmed=false` for review.

The rewrite covers: the FilenameParser as a SAGE-provided library; the `/parse-filename` endpoint and corresponding MCP tool that exposes it for callers that want suggestions before commit; the precedence chain (caller > filename parse [only when needs_review=true] > chain inheritance > vault default); the chain-inheritance exception (when `supersedes_document_id` is set, the predecessor's `doc_type`, `project`, and `authority_scope` are inherited per-field if the caller did not supply them); and the deprecation of `metadata_extraction.review_required` (removed in substrate v1.12).

Cite ADR-015 and ADR-021.

##### §3.4.7 Edge Inference

**REPLACE wholesale.** Three changes:

- The three-tier model (auto-create / auto-suggest / manual only) stays in shape but the text is rewritten to acknowledge that the inference engine lives in `app/backend/edge_inference.py`, not in SAGE itself. This is a boundary clarification: the engine consumes SAGE state and produces edge plans; SAGE provides the link primitive and the staging table.
- **Mechanical-vs-curated provenance gate (ADR-019).** Edges authored by version_chain inference carry a leading `[version_chain]` rationale prefix. Chain-repair operations may delete prior auto-inferred edges to reshape a chain when an out-of-order version arrives, but if any candidate-removal edge is hand-curated (lacks the prefix), the entire group's repair downgrades to Tier 2 staging with the conflicting hand-curated edge left untouched. Group-level downgrade preserves repair coherence.
- **Linear supersedes chain.** Each version supersedes its immediate actual predecessor, not all prior versions. Currently a tacit design decision flagged in the project tracker but not yet captured as an ADR. The rewrite states the decision explicitly. Cite candidate ADR (Linear supersedes chain).

Cite ADR-019 and the candidate linear-supersedes ADR.

##### §3.4.8 UI-Layer File Metadata Normalization

**NEW.** Short subsection (3-4 sentences) describing the macOS-specific sanitization step (per ADR-016): the BSD UF_HIDDEN chflag and the com.apple.FinderInfo invisible bit are stripped from files copied into the vault, preserving all other Finder metadata (color labels, type/creator codes). No-op on non-macOS platforms. Errors are swallowed; UI-layer sanitization is best-effort and must not fail an ingest.

#### §3.5 Client Access Architecture

##### §3.5.1 Protocol Adapters and Transports

**REVISE.** The current table lists Claude/MCP, ChatGPT/Gemini, human app, and future clients. Update to current state:

- MCP adapter: stdio transport (legacy, still works), SSE transport via `mcp-remote` bridge to the FastAPI server's `/mcp/sse` mount (current default since 2026-04-14, eliminates the duplicate process Cowork previously spawned).
- REST API: FastAPI / Swagger UI on `localhost:8000` for direct programmatic access.
- CAS Application: React SPA served by the same FastAPI process at the application backend's URL prefix (`/app/*`); see §3.5.2.

##### §3.5.2 Single-Process Topology

**NEW.** The Phase 1 architectural choice is one uvicorn process serving the SAGE Core API (`/sage_vaults/*`), the CAS Application backend (`/app/*`), the React SPA as static assets, and the FastMCP SSE mount (`/mcp`) — all sharing the same `app.state.vault_registry`, the same SAGE service instances, and the same SQLite/LanceDB connections. Code is structured as if the components communicated over HTTP so that Phase 2 separation (CAS app on its own process, optionally on a different machine) requires no architectural rework. Cite candidate ADR (Single-process topology).

### §4 Object Model

#### §4.1 Document

**REVISE.** The Document table needs new fields:

- `semantic_abstract` (already in v1.4.2 from ADR-011) — keep with cross-reference to §3.4.1 stage 3.
- `pipeline_status` — enum (projection_complete, indexing_in_progress, indexing_complete, abstraction_in_progress, abstraction_complete, abstraction_skipped, failed). Surfaces ingestion state.
- `pipeline_error` — nullable string. Failure description when pipeline_status is failed.
- `metadata_confirmed` — boolean. Set by the ingest path per ADR-021.
- `document_date` — nullable string (YYYY-MM-DD). Authoritative content date derived from filename date code or `source_modified_at` fallback.
- `source_modified_at` — nullable timestamp. Source file's filesystem modification time at ingestion (distinct from `created_at`, which is SAGE's ingestion timestamp).
- `indexed_at` — nullable timestamp (was non-null in v1.4.2; substrate v1.1 made it nullable).

#### §4.2 User

**STAY.**

#### §4.3 Editor Model (Access Control)

**STAY.** Note: editor endpoints (GET/PUT) remain forward-declared in OpenAPI per the spec backfill (substrate v1.10) but are not yet implemented; verify and add a sentence.

#### §4.4 Metadata Model

**REVISE.** Three-tier structure stays. Tier 1 enumeration updated to match the revised Document table (§4.1).

#### §4.5 Edge (Graph Relationship)

**REPLACE.** Edge schema gains substantial fields per ADR-017:

- `id` — auto-generated UUID (substrate v1.1).
- `resolution_policy` — enum (none, transitive_source, transitive_target, transitive_both, TBD), frozen at edge creation.
- `source_valid_from_version` — nullable document_id reference (anchor on source chain).
- `target_valid_from_version` — nullable document_id reference (anchor on target chain).
- `valid_until_version` — nullable; used exclusively for merge-triggered tombstoning.
- `retracted_edge_id` — nullable; carries the edge_id of the retracted edge for `retracts` rows.
- `target_id` — relaxed to nullable to accommodate `retracts` rows.

Cite ADR-017. Worked example moved to Appendix D; the section text refers the reader there.

#### §4.6 Edge Type Taxonomy

**REPLACE.** Update the table to the 11 production edge types, each with its default `resolution_policy`:

| Edge Type | Resolution Policy | Semantics |
|---|---|---|
| supersedes | none | Version lineage |
| retracts | none | Governance-level retraction of an edge instance |
| merged_from | none | Chain-level provenance after a chain merge |
| derived_from | transitive_source | Structural inheritance (template traceability) |
| instantiated_from | transitive_both | Live-tracking derivation (template instantiation) |
| references | transitive_both | Loose association |
| covers | transitive_both | Oversight mapping |
| bundles_with | transitive_both | Co-traveling artifacts |
| depends_on | transitive_both | Precondition gate |
| authoritative_for | TBD | Source authority boundary |
| sync_target | TBD | Bidirectional sync (deferred policy) |

Cite ADR-017.

#### §4.7 Edge Type Registry and Resolution Model

**NEW.** Substantive subsection (~250-400 words) describing:

- The per-vault edge type registry that assigns each used edge type a `resolution_policy` from the four-valued enumeration, plus the `TBD` governance placeholder.
- The write-time invariant that gates anchor-field population: `transitive_source` requires `source_valid_from_version`; `transitive_target` requires `target_valid_from_version`; `transitive_both` requires both; `none` forbids both.
- The four resolution-path event types surfaced under the traversal debug flag: `anchor_hit`, `anchor_miss`, `retracts_applied`, `tombstone_applied`.
- The legacy edge handling: edges with NULL `resolution_policy` (pre-ADR-017 data) are treated as `policy=none` at resolution time; data migration is documented but not auto-run.

Cite ADR-017 and tracker note v0.9.13 (legacy edge visibility under chain-scoped resolution).

#### §4.8 Vault

**STAY.** Path layout updates flow into §8.3.

#### §4.9 Lifecycle State Machine

**REVISE.** Remove the `superseded` state from the base set. Per the lifecycle simplification 2026-04-14: `supersede` action now transitions directly to `archived`. The `supersedes` edge (which carries the version relationship) is still created. Updated transition table:

| From | Action | To |
|---|---|---|
| (new) | ingest | active |
| active | supersede(new_version_id) | archived (and supersedes edge created) |
| active | complete() | completed |
| active | archive() | archived |
| completed | archive() | archived |
| archived | reactivate() | active |

Note that vaults extend the base state set per their domain configuration; the base set is the SAGE-enforced minimum.

#### §4.10 Decision Logs

**NEW.** Subsection (~150-250 words) per ADR-012: stewards and orchestrators may each own an append-only decision log as a SAGE-managed companion artifact. Three categories of entry: decisions not to act, decisions deferred, cross-artifact reasoning. Supersession is by back-reference (parallel to the ADR supersession pattern). The schema is part of the formal substrate (`sage/decision_log.schema.json`). Domain instantiations decide which agents receive decision logs.

Note: ROOT Harness is not yet built, so the orchestrator-log surface is forward-looking; the steward-log surface is the relevant near-term consumer. The section frames the architecture for both without conflating implementation status. Cite ADR-012.

#### §4.11 Pipeline Status

**NEW.** Short subsection describing the pipeline_status field on Document, the enum values, and the terminal-status set. This is referenced from §3.4.1 and §4.1; the section is the single authoritative place for the enumeration.

### §5 Retrieval Architecture

#### §5.1 Retrieval Modes

**REPLACE.** The current document lists semantic, deterministic, and verification. Code reality (`sage/models/enums.py`): semantic, keyword, deterministic, catalog. `verification` is not a code mode; the document's verification example (compare authoritative source to consumer) is implemented as a workflow on top of two deterministic retrievals, not a distinct API mode. The rewrite drops `verification` from the retrieval-mode enumeration and reframes the verification example as a usage pattern composed from deterministic retrievals.

Updated table:

| Mode | Guarantee | Mechanism | Use Case |
|---|---|---|---|
| semantic | Approximate; ranked by relevance | Vector similarity, optionally fused with BM25 keyword | Discovery, lessons-learned analysis |
| keyword | BM25 only | Tantivy full-text index | Identifier search, formal terminology |
| deterministic | Exact content extraction | Direct content extraction by document_id + heading_path | Verbatim extraction; governed content transfer |
| catalog | Filter-only enumeration; no vector search | SQL enumeration over graph store with metadata pre-filter | Browsing, document-list queries |

Note in the prose that hybrid (semantic + BM25 fusion) is not a separate mode but a flag on semantic mode; cite the substrate manifest.

#### §5.2 Source Scope

**STAY.**

#### §5.3 Combined Retrieval Parameters

**REVISE.** Replace the verification example with a deterministic-only example. Add catalog and keyword examples to the parameter combinations.

#### §5.4 Hybrid Search and Reranking

**REVISE.** Hybrid search prose stays. Add a sentence acknowledging that "reranking" now spans more than the original BM25 fusion; see §5.5.

#### §5.5 Salience Reranking

**NEW.** Substantive subsection per BH-069 and BH-070 (tracker, 2026-04-11). Two boosts applied after content scoring:

- **Active-lifecycle tier sort.** Documents with `lifecycle_status="active"` are tier 0; everything else is tier 1. Active documents always rank above non-active regardless of content score.
- **Recency boost.** Up to 1.10x via exponential decay with a 365-day half-life. Date resolution priority: `document_date` > `source_modified_at`.

Note that the original 1.15x active-lifecycle multiplier was replaced by the tier sort on 2026-04-14 to avoid edge cases where a high-scoring archived document outranked a relevant active document.

#### §5.6 Abstract-Boosted Retrieval

**NEW.** Per ADR-011 and the v1.6 substrate update (2026-04-14): two-pass abstract-boosted retrieval. Documents whose semantic_abstract matches the query receive a multiplicative score boost. Documents without abstracts remain discoverable at natural chunk-relevance scores. Controlled by `DiscoverRequest.use_abstract_prefilter` (default true). Applies to semantic and keyword modes; ignored by catalog and deterministic.

The semantic abstract prompt is shaped per ADR-020: descriptive third-person framing, doc_type as a parameterized input, anti-fabrication clauses. This is operationally relevant to retrieval because retrieval quality depends on abstract quality; cite ADR-011 and ADR-020.

#### §5.7 Document-Level Response Mode

**NEW.** The `response_level` parameter on DiscoverRequest controls result granularity: `chunks` (default; chunk-level hits with content and heading_path) or `documents` (document-level hits suppressing chunk content but preserving heading_path of best chunk and relevance scores). Applicable to semantic and keyword modes; ignored by catalog (always document-level) and deterministic (always chunk-level).

#### §5.8 Pre-Filter Resolution

**NEW.** Per the 2026-05-04 fix: document-level filters (`lifecycle_status`, `project`, `tags`, `pipeline_status`) are resolved into a `document_id IN (...)` set against the graph store before chunk search runs in the content store. Without this, the LanceDB top-K could return all-archived chunks that the post-search filter then culled to zero. Doc_type pre-filter operates at the chunks layer (chunks now carry `doc_type` as a column).

#### §5.9 Chain Walk Operation

**NEW.** Per substrate v1.5: `sage_chain` walks an edge chain to both ends from any starting document via a recursive CTE. Returns `head_id`, `tail_id`, `query_position`, `length`, and `is_linear` (fork detection). Designed for version-history retrieval on long supersedes chains but works with any edge type. Distinct from `sage_traverse` because chain walk is bidirectional from a midpoint with no depth bound.

### §6 Access Control and Governance

**REVISE** lightly:

- §6.4 No-Delete Invariant: clarify that the invariant applies to documents only. Edges may be removed via `unlink` (corrected in substrate v1.10).
- §6.5 Provenance Tracking: add the `metadata_confirmed` flag (per ADR-021) and a brief reference to decision logs (§4.10) as a complementary mechanism.

Otherwise STAY.

### §7 Core API Operations

**REPLACE wholesale.** The current single-table presentation lists 14 operations. The current API is 28+ SAGE operations plus the CAS Application API surface. Restructure as:

#### §7.1 Operation Catalog by Tag

Group operations by OpenAPI tag (matching the substrate spec organization):

- **Discovery.** `discover`, `chain` (chain walk), `traverse`, `check_preconditions`.
- **Documents.** `ingest`, `get_document`, `set_lifecycle`, `update_metadata`, `parse_filename`, projections (`export_projection`, projection/section reads).
- **Graph.** `link`, `unlink`, edge-type registry queries.
- **Vault Management.** `list_vaults`, `create_vault`, `vault_stats`, `get_vault_config`, `update_vault_config`, `reload_vault`, `hash_check`, `refresh_views`, `eval_retrieval`.
- **Staging Edges.** `list_staging_edges`, `confirm_staging_edge`, `dismiss_staging_edge`.
- **Pending Metadata.** `list_pending_metadata`.
- **Users and Editors.** `register_user`, `set_editors`, `get_editors`.

Each operation entry: name, signature, purpose, backing store, governance/policy notes where relevant.

Cite ADR-014 for the named-operation pattern (set_lifecycle / update_metadata replacing organize), ADR-017 for traversal anchor and retraction semantics, ADR-021 for parse_filename.

#### §7.2 Application API Surface

**NEW.** The CAS Application API at `/app/*` is architecturally distinct from the SAGE Core API at `/sage_vaults/*`. Two endpoints: `POST /app/scan` (recursive directory walk, hashing, adapter detection, hash-check against vault, parsed metadata via FilenameParser) and `POST /app/ingest` (per-file SAGE ingest with two-phase edge inference, SSE-streamed progress, batch summary). The architectural rationale is that filesystem walking and bulk orchestration are caller-side concerns that SAGE deliberately does not own; cite the boundary rule and ADR-018.

### §8 Multi-Vault Architecture

**STAY** with §8.3 path correction.

#### §8.3 Physical Layout

**REPLACE the path layout.** The current document references `~/vault/.brain/...`. Reality:

- `~/sage_vaults/{vault_id}/` — vault root
- `~/sage_vaults/{vault_id}/vault_config.yaml` — vault configuration (validated against `sage/vault_config.schema.json` from the formal substrate)
- `~/sage_vaults/{vault_id}/brain/` — database directory
- `~/sage_vaults/{vault_id}/brain/graph.db` — graph store (SQLite)
- `~/sage_vaults/{vault_id}/brain/lancedb/` — content store (LanceDB)
- `~/sage_vaults/{vault_id}/sources/` — source files (may be symlinked to external storage)
- `~/sage_vaults/{vault_id}/imports/` — staging directory for files copied during ingest

Vault configs live outside the repository; this is intentional per the "stale-state pitfalls" guidance in CLAUDE.md.

### §9 Technology Stack Summary

**REVISE.** Update the table to current production realities:

- Embedding model: nomic-embed-text-v1.5 (768d), CPU device, max_seq_length=2048, batch_size=8.
- Content store: LanceDB embedded; manual compaction required for vault-rewrite operations.
- Graph store: SQLite with thread-local connection pool (bounded ThreadPoolExecutor, default 4 workers), WAL mode, foreign keys enabled, recursive CTE support.
- Abstraction model: Qwen3-30B-A3B-Instruct-2507-4bit via MLX on Apple Silicon. Lazy load on first generate_abstract call. Greedy decoding (deterministic). Context-window-aware input truncation.
- Core API: FastAPI / uvicorn.
- MCP transport: stdio (legacy) or SSE via mcp-remote bridge to `/mcp/sse` (current default).
- Adapters: markdown (native), docx (with .dotx template-overload), xlsx, pdf (native-text only).
- Indexing: on-demand only (no file watcher per ADR-018).

### §10 Open Design Questions

**PRUNE and UPDATE.** Items currently listed:

1. Provenance edge vs. provenance fields (Phase 3) — STAY.
2. Read-access restrictions (Phase 3) — STAY.
3. Interactive scope boundary — RESOLVED (separate-agent-with-separate-context pattern adopted across the project).

Add new open questions surfaced during the build:

4. **Periodic LanceDB compaction.** Production `IngestionService.ingest()` does not compact; over months of normal ingest activity fragments accumulate. A maintenance task or per-N-ingests trigger is warranted. Currently a flagged follow-up in the project tracker.
5. **`sync_target` and `authoritative_for` resolution policies.** Currently TBD in the edge type registry. Both deferred per ADR-017.
6. **`tags` chain inheritance.** Excluded from ADR-021's chain-inheritance trio because set semantics make "inherit" ambiguous (union, replace, skip). If a concrete need surfaces, a separate decision will be required.
7. **Steward and orchestrator decision-log activation.** Schema exists per ADR-012; first consumer (steward) awaits domain instantiation choices. Orchestrator log awaits ROOT Harness implementation.

### Appendix A: Agent Consumption Patterns

**STAY** with light edit — verify the patterns described still match how agents are using SAGE in practice (e.g., the abstract-as-triage-card pattern from ADR-020 is now empirically validated).

### Appendix B: Revision History

**APPEND v2.0 entry** summarizing this rewrite: "Comprehensive refresh to as-built state. Substantive incorporation of ADRs 011-021 and substrate revisions v1.5-v1.14. Edge model updated for chain-scoped resolution (ADR-017). Source change detection rewritten to remove file watcher (ADR-018). Metadata extraction rewritten for caller-owned model with chain inheritance (ADR-021). Edge inference rewritten with mechanical-vs-curated provenance gate (ADR-019). Retrieval architecture expanded with catalog and keyword modes, salience reranking, abstract-boosted retrieval, document-level response mode, pre-filter resolution, and the chain walk operation. Lifecycle state machine simplified (superseded state removed). Core API operations restructured by tag (28+ operations). Multi-vault physical layout corrected. Pipeline status surface added. Decision logs section added (ADR-012). UI-layer file metadata normalization documented (ADR-016). New appendix D contains a worked example of chain-scoped edge resolution. New CAS-ADRs introduced alongside this revision: candidate sequential-pipeline ADR, candidate lazy-MLX-load ADR, candidate single-process-topology ADR, candidate linear-supersedes-chain ADR. Filename version bump from v1.4.2 (filename) / v1.4 (tracker) to v2.0; the inconsistency in v1's labeling resolves into a clean v2.0."

### Appendix C: CAS ADR Index

**APPEND ten new entries** with one-paragraph summaries each:

- ADR-011 (already in v1.4.2 — confirm and revise): Semantic abstract generation at ingestion.
- ADR-012: Decision logs as SAGE-managed companion artifacts.
- ADR-013: Typed event stream for agent execution observability (light SAGE relevance via observability hooks; primarily ROOT Harness).
- ADR-014 (already in v1.4.2): Named Core API operations.
- ADR-015: Metadata extraction is a SAGE-level capability.
- ADR-016: SAGE normalizes UI-layer file metadata on ingest.
- ADR-017: Chain-scoped edge resolution with anchor and retraction primitives.
- ADR-018: Ingestion is exclusively intentional.
- ADR-019: Auto edge inference may delete its own prior edges; hand-curated edges protected.
- ADR-020: Abstract generation prompt — descriptive framing for agent triage.
- ADR-021: Metadata inference and review are caller responsibilities.

Plus the new ADRs candidate-drafted alongside this rewrite (see ADR draft candidates section below).

### Appendix D: Worked Example — Chain-Scoped Edge Resolution

**NEW.** Adapt the worked example from ADR-017's rationale section: chains A (a1-a9) and B (b1-b5), a covers edge with `transitive_both` policy, anchored at (a3, b2); a retracts edge anchored at a7; chain A merging into chain C with `merged_from`. Walk through the resolver behavior at three query times. This is the most efficient way to convey the model to a reader who has not internalized chain-scoped resolution; the appendix format keeps it out of the main text while keeping it close.

## ADR draft candidates surfaced by this rewrite

Per session decision (ADRs reserved for substantive decisions whose preservation prevents drift from architect intent), the following are flagged as candidates to draft alongside the v2.0 rewrite. Final disposition is yours.

1. **Sequential pipeline.** All three ingestion stages complete before `ingest()` returns. Driven by memory exhaustion under bulk ingest; load-bearing for the 64 GB envelope. Replaces the prior `asyncio.create_task` fire-and-forget pattern. **Recommend draft.**

2. **Lazy MLX abstraction model loading.** Qwen3-30B loads on first `generate_abstract()` call rather than at service startup. Saves ~16-20 GB baseline RAM for vaults that have not yet generated an abstract. **Recommend draft.**

3. **Single-process Phase 1 topology.** SAGE Core API + CAS Application backend + FastMCP SSE mount in one uvicorn process, sharing the vault registry and service instances. Phase 2 separation is explicit forward intent; code is structured as if the components communicated over HTTP. **Recommend draft.**

4. **Linear supersedes chain.** Each version supersedes its immediate actual predecessor, not all prior versions. Currently a tacit decision flagged in the project tracker as an architecture correction needed; not yet captured as an ADR. **Recommend draft.**

If you approve drafting, my plan is to produce ADR drafts as part of the SAGE Arch v2.0 work and surface them for your review before they land in `docs/cas_adr_store.json`. Each draft would carry candidate IDs ADR-022, ADR-023, ADR-024, ADR-025; final IDs assigned at ratification.

## Open decision points

1. **Lifecycle state machine table.** I propose removing `superseded` from the base state set entirely (as the lifecycle simplification 2026-04-14 did) and showing the supersede action as transitioning directly to `archived`. Confirm or push back.

2. **Retrieval mode `verification`.** I propose dropping `verification` as a top-level retrieval mode and reframing the verification example as a usage pattern over two deterministic retrievals. Code does not have a `VERIFICATION` enum value; the document's mode should match. Confirm or push back.

3. **Edge type table reorganization.** I propose grouping edge types by resolution policy (with the meta-edges supersedes/retracts/merged_from at the top) rather than by alphabetical order. Better for reader comprehension. Confirm or push back.

4. **Worked example placement.** I propose Appendix D for the chain-scoped edge resolution worked example. Alternative: inline within §4.7 (Edge Type Registry and Resolution Model). I prefer the appendix because the example is dense and breaks the main flow. Confirm or push back.

5. **ADR draft scope.** Proposed: four candidate ADRs (sequential pipeline, lazy MLX, single-process topology, linear supersedes). Plus the documentation-as-vault candidate ADR-022 from the inventory (parallel work, not part of SAGE Arch). Confirm number and scope.

6. **Open Design Questions §10.** I propose pruning items 3 and adding items 4-7. Items 1 (provenance edge) and 2 (read-access restrictions) stay. Confirm or push back.

If the plan is acceptable as proposed, `;;yp` to start authoring SAGE Architecture Reference v2.0. The plan is to produce a fresh `.docx` derived from the existing template, conforming to the Formatting Standards, with the rewritten content. The draft will be shared via `computer://` link for your review before it is treated as a delivery.
