# CAS Reference Documentation Drift Inventory

**Date:** 2026-05-06
**Author:** Clif (with Cowork assistance)
**Status:** Draft for review before any .docx edits begin

## Purpose

This inventory identifies the gap between (a) the seven CAS reference documents in `docs/ref/` as currently authored and (b) the as-built state of SAGE and the CAS Application, plus the as-designed intent for ROOT Harness. It is the audit deliverable that gates the documentation refresh pass: no .docx edits begin until this inventory is reviewed and accepted.

The standard adopted for the refresh (per session decision 2026-05-06) is that a human reader should be able to read the reference documents alone and accurately understand what CAS is and how it works for the parts that are built, plus what CAS is intended to be for the parts not yet built. The reader should not be required to consult the code, the commits, or the ADRs. ADRs continue to function as the audit trail and conflict-resolution authority but the documents must stand alone.

## Audit basis

This inventory was produced from the following authoritative sources:

| Source | State at audit time |
|---|---|
| ADR store (`docs/cas_adr_store.json`) | 21 ADRs, ADR-001 through ADR-021, dates 2026-03-16 to 2026-05-01 |
| Formal Substrate manifest (`docs/fs/manifest.json`) | substrate v1.14, 2026-05-02 |
| Project Tracker (`CAS_Project_Tracker.md`) | v91, last updated 2026-05-05 |
| .docx files in `docs/ref/` | Last touched 2026-03-30 through 2026-04-08 |

ADRs that post-date the most recent .docx file (App-Spec, 2026-04-08): ADR-015 (2026-04-16), ADR-016 (2026-04-16), ADR-017 (2026-04-18), ADR-018 (2026-04-25), ADR-019 (2026-04-29), ADR-020 (2026-05-01), ADR-021 (2026-05-01). Substrate revisions v1.5 through v1.14 likewise post-date all of the .docx files.

## Per-document classification key

For each document, drift is classified as:

- **D — Drift.** The document claims X; reality is Y. Resolution: rewrite to reflect reality.
- **R — Missing rationale.** The decision is reflected in code but the why-it-is-so was captured only in an ADR or in commit history. Resolution: incorporate the rationale into the prose with a citation to the ADR ID.
- **S — Missing structure.** A component, interface, or concern that a self-contained reader would need to understand CAS but that the original document did not cover. Resolution: write a new section.
- **N — No drift.** Verified consistent with current state.

Each item also carries a remediation classification: **replace**, **incorporate-with-citation**, **new-section**, or **no-change**.

## Summary table

| Doc | Built? | Drift severity | Estimated scope | Approach |
|---|---|---|---|---|
| 1. SAGE Architecture Reference (v1.4.2) | Yes | High | Heavy revision across §3-§7 plus §9-§10 | As-built rewrite incorporating ADRs 011-021 and substrate v1.5-v1.14 |
| 2. CAS Application Spec (v0.4) | Yes | Medium-High | Section-level updates across most views, plus new operational notes | As-built rewrite incorporating six-view production state and ADR-021 caller-owned metadata |
| 3. CAS Overview (v1.3) | Mixed | Medium | §4 (System Components) needs status reflecting build state; §5-§6 need version updates | Hybrid: as-built for SAGE/app sections, prescriptive for ROOT Harness sections |
| 4. ROOT Harness Architecture Reference (v1.0) | No | Medium (design-driven) | Forward-looking design refresh in light of SAGE/app reality and tracker open considerations | Prescriptive design refresh, incorporating sprint contract pattern, generator/evaluator separation, context reset over compaction |
| 5. Deployment Model (v1.0) | Mixed | Low-Medium | §5-§7, §10-§11 need MCP setup, MLX realities, vault paths, LanceDB compaction | Hybrid: as-built for SAGE/app deployment, prescriptive for ROOT Harness deployment |
| 6. System Architecture Diagram (PNG, 2026-04-06) | Mixed | Medium | Re-render after textual docs stabilize; legend distinguishing built from planned | New PNG with built-vs-planned visual convention |
| 7. Formatting Standards (v1.0) | n/a | Low | Verify only; no known drift | Likely no-change |

## Per-document inventory

### 1. SAGE Architecture Reference (v1.4.2; filename suggests v1.4.2, tracker tracks v1.4 — version label inconsistency to flag)

**Current section list:** Introduction; Conceptual Foundations; System Architecture; Object Model; Retrieval Architecture; Access Control and Governance; Core API Operations; Multi-Vault Architecture; Technology Stack Summary; Open Design Questions.

#### §3 System Architecture

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.3.1 | D | §3.4.5 "Source Change Detection" describes a file watcher as a "Phase 1 design decision," with periodic hash comparison as fallback for sources the watcher cannot observe. ADR-018 (2026-04-25) formally excludes any file watcher, polling loop, or auto-ingest. Ingestion is exclusively intentional via `sage_ingest`, `app_scan_directory`, or `app_batch_ingest`. Source change detection happens only when a caller re-ingests a known path. | Replace. Rewrite the section to describe the intentional-ingestion model with hash comparison performed at re-ingest time and `supersedes`-linked successor creation on content change. Cite ADR-018. |
| 1.3.2 | D | §3.4.7 "Edge Inference" predates ADR-017 (chain-scoped edge resolution) and ADR-019 (mechanical-vs-curated provenance gate). Three-tier model in the doc still holds in shape but the semantics under chain-scoped resolution and the provenance gate require the section to be rewritten. | Replace. Cite ADR-017 and ADR-019. |
| 1.3.3 | R | §3.4.6 "Metadata Extraction" predates ADR-015 (metadata extraction is a SAGE-level capability), ADR-016 (UI-layer metadata normalization on ingest), and ADR-021 (caller-owned metadata with chain-inheritance exception). The mechanism is implemented; the rationale and the precedence chain (caller > filename parse [only when needs_review=true] > chain inherit > vault default) are absent from the doc. | Incorporate-with-citation. Rewrite to describe the FilenameParser as a SAGE-provided library, the `needs_review` boundary, and the chain-inheritance exception for the (doc_type, project, authority_scope) trio. Cite ADRs 015, 016, 021. |
| 1.3.4 | S | The architecture section does not currently describe the MCP server / SSE transport mount on the FastAPI app, the multi-vault registry pattern, or the relationship between the SAGE process and the CAS Application backend (one uvicorn process, two URL prefixes `/sage_vaults/*` and `/app/*`). A reader cannot understand from this doc how clients reach SAGE. | New section or expand §3.5 Client Access Architecture. Describe stdio MCP, SSE transport via `mcp-remote` bridge, REST API, and the single-process / shared-vault-registry topology. |
| 1.3.5 | S | The doc describes the ingestion pipeline at a layer-name level but does not describe the sequential pipeline behavior (Stages 1-3 complete before ingest returns, lazy MLX load, embedding-on-CPU memory footprint) that is now load-bearing for operational reasoning. | New subsection under §3.4 (Ingestion). Describe the sequential pipeline guarantees and their resource implications. |

#### §4 Object Model

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.4.1 | D | The edge model predates ADR-017 (chain-scoped edge resolution). Edges now carry `resolution_policy` (frozen at creation), `source_valid_from_version`, `target_valid_from_version`, `valid_until_version`, and `retracted_edge_id`. New edge types: `instantiated_from`, `retracts`, `merged_from`. Per-vault edge type registry assigns resolution policies. | Replace the edge model section. Add the registry, policy enumeration, and the four event types in resolution paths (anchor_hit, anchor_miss, retracts_applied, tombstone_applied). Cite ADR-017 and ADR-019. |
| 1.4.2 | D | The lifecycle state machine in the doc retains `superseded` as a base state. The state was removed 2026-04-14 (lifecycle simplification); supersede now transitions directly to `archived`. | Replace the lifecycle subsection. |
| 1.4.3 | R | Document record now includes `semantic_abstract`, `document_date`, `source_modified_at`, `pipeline_status`, `pipeline_error`, `metadata_confirmed`, `indexed_at` (nullable), and adapter-contributed tags. None of these are documented. | Incorporate-with-citation. Cite ADR-011 (semantic abstract) and the substrate manifest for the rest. |
| 1.4.4 | S | "Decision logs" as a SAGE-managed companion artifact (CAS-ADR-012) is not in the doc. Decision logs are stewarded artifacts captured per agent and surfaced through the same retrieval mechanisms; they belong in the Object Model. | New subsection. Cite ADR-012. |

#### §5 Retrieval Architecture

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.5.1 | D | RetrievalMode in the doc covers semantic, hybrid, deterministic. Current code adds `catalog` (filter-only enumeration) and `keyword` (BM25-only). | Replace §5.1. |
| 1.5.2 | S | Document-level response mode (`response_level: chunks | documents`) is absent. | New subsection. |
| 1.5.3 | S | Abstract-boosted retrieval (two-pass abstract prefilter, `use_abstract_prefilter` flag) is absent. Cite CAS-ADR-011 and ADR-020. | New subsection. |
| 1.5.4 | S | Salience reranking (active-lifecycle tier sort plus exponential recency decay) is absent. | New subsection or fold into §5.4 Hybrid Search and Reranking. |
| 1.5.5 | S | The `sage_chain` operation (chain walk to both ends of a supersedes chain) is absent. The supersedes chain is now linear by design (architecture correction flagged in tracker §"Flagged for Discussion"). | New subsection. |
| 1.5.6 | D | Catalog mode sort parameters (`sort_by`, `sort_order`), pagination (`offset`, `total_available`), and filter behavior (`pipeline_status` override, hard tag AND-filtering) are not documented. | Incorporate into §5. |
| 1.5.7 | D | Document-level pre-filter resolution (lifecycle_status, project, tags, pipeline_status resolved to a `document_id IN (...)` set before chunk search) is absent. The doc still implies post-filter only. | Replace the relevant subsection. |
| 1.5.8 | D | Doc_type pre-filter at the LanceDB layer (chunks now carry `doc_type`; pre-filter applied before scoring) is absent. | Incorporate. |

#### §6 Access Control and Governance

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.6.1 | D | The metadata-confirmed semantics changed under ADR-021. The doc describes a vault-config-driven review queue; the current model is caller-driven via `IngestRequest.needs_review`, with the prior `metadata_extraction.review_required` field deprecated and removed (substrate v1.12). | Replace. Cite ADR-021. |
| 1.6.2 | S | "No-delete invariant" applies to documents only; edges may be removed via unlink. The doc may not have made this distinction explicit. | Verify; incorporate as needed. |

#### §7 Core API Operations

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.7.1 | D | The doc lists 14 operations (per substrate v1.0, 2026-03-31). Current Core API is 28+ operations with the v1.10 backfill (vault listing/creation, stats, hash-check, staging-edges list/confirm/dismiss, pending-metadata, document open/projection/section reads, edge unlink), plus the chain walk endpoint, plus the `/parse-filename` endpoint from ADR-021. Plus the separate CAS Application API at `/app/*` (scan, batch ingest). | Replace §7 wholesale. Reorganize by tag. |
| 1.7.2 | D | `organize()` is no longer in the API; decomposed into `set_lifecycle()` and `update_metadata()` per ADR-014. The doc may still mention it. | Verify; replace if present. |
| 1.7.3 | S | The split between `sage_core_api` (`/sage_vaults/*`) and `cas_app_api` (`/app/*`) is architecturally significant and not documented. | New subsection or table at the top of §7. |

#### §9 Technology Stack Summary

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.9.1 | R | The local environment realities (Apple Silicon, 64 GB unified memory, Qwen3-30B via MLX, embedding-on-CPU, lazy MLX load) are captured in CLAUDE.md but not in the architecture reference. A reader should understand the resource envelope and why it's where it is. | Incorporate-with-citation. Possibly cross-reference the Deployment Model. |

#### §10 Open Design Questions

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.10.1 | D | Several "Phase 3" deferrals likely have moved (e.g., access control structures around editors are still forward-declared but documented in the OpenAPI spec; provenance-edge-vs-fields is still open). | Audit each item; mark closed items closed; carry remaining open items into a §10 of the new revision. |

#### §3 / §6 — paths

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.path.1 | D | Doc references `~/vault/.brain/...` directory structure. Actual layout is per-vault under `~/sage_vaults/{vault_id}/brain/{graph.db, lancedb/}` with sources optionally symlinked under the vault. | Replace path references throughout. |

### 2. CAS Application Spec (v0.4)

**Current section list:** Table of Contents; Introduction; Technology Stack; Navigation Structure; Vault Dashboard; Ingest View; Metadata Review; Edge Review; Search; Document Detail; Graph Explorer; Revision History.

| ID | Type | Item | Remediation |
|---|---|---|---|
| 2.1 | D | The spec is at v0.4 (technology stack revision to React SPA + FastAPI). Six views (Dashboard, Ingest, Review, Search, Document Detail, Graph Explorer) are now in production with API integration, error handling, vis.js graph rendering, SSE-driven progress, and many bug fixes. The spec predates all of this. | As-built rewrite per view. |
| 2.2 | D | Search view: catalog mode and Browse, sortable columns (Title, Type, Date, Status), pagination, document-level response mode, abstract surfacing, lifecycle filter dropdown, filter resolution behavior — all post-spec. | Replace Search section. |
| 2.3 | D | Dashboard: ten statistics + four health indicators, BreakdownCards, adapter registry derived from runtime registry, deferred abstract semantics under abstraction.enabled, LanceDB storage path correction. | Replace Dashboard section. |
| 2.4 | D | Ingest view: two-phase edge inference (pre-ingest plan, post-ingest resolution), SSE event format, per-file error isolation, edge_type list, version_chain doc_type tightening, filename parser pre-split + post-split phases, project_identifier field. | Replace Ingest section. |
| 2.5 | D | Metadata Review: under ADR-021 the bulk-ingest path passes `needs_review=true` explicitly; the queue is populated by SAGE only when caller asks for review. The behavior of the review surface needs description in those terms. | Replace. Cite ADR-021. |
| 2.6 | D | Edge Review: the staging surface needs the ADR-019 mechanical-vs-curated UX so the reviewer sees the conflicting existing edge alongside the proposed new one (flagged in tracker; UX may not yet be built — confirm). | Verify implementation status; either describe as built or note as forward-looking. |
| 2.7 | S | MCP tool surface (20 tools total) is not described in this spec. A reader who wants to understand programmatic access via MCP has to consult code. | New section. |
| 2.8 | S | Operational notes: LanceDB compaction issue and recovery (2026-05-04); 60s MCP ceiling and its implications; abstract regeneration via `sage_reabstract`; vault management (create, update_config, reload). | New section. |
| 2.9 | R | The architectural decision that directory scan and batch ingest are application-layer (not Core API) operations is captured in the tracker but should be incorporated as substantive prose in the spec, not just listed. | Incorporate. |

### 3. CAS Overview (v1.3)

**Current section list:** Introduction; Goals; Design Principles; System Components (with subsections for SAGE, ROOT Harness, Boundary Rule, CAS Application, Domain Instantiations); Ecosystem Artifacts; Document Portfolio and Reading Guide.

| ID | Type | Item | Remediation |
|---|---|---|---|
| 3.1 | D | §4.1 SAGE: needs to reflect that SAGE is at "beta-level production" (per tracker step 20, 2026-04-13), with all three production adapters delivered, MCP server live with 20 tools, FastAPI server live, multi-vault, etc. The Overview need not reproduce SAGE Architecture Reference content but should accurately characterize state. | As-built rewrite. |
| 3.2 | D | §4.2 ROOT Harness: not built. The Overview should reflect this clearly (prescriptive section, with implementation status noted). | Edit to mark as not-yet-built; cross-reference ROOT Harness Architecture Reference for design. |
| 3.3 | D | §4.4 CAS Application: needs to reflect six views in production, React SPA + FastAPI backend, single-process topology with `/app/*` and `/sage_vaults/*` URL prefixes. | As-built rewrite. |
| 3.4 | S | §4.3 Boundary Rule applies between ROOT Harness and SAGE; with SAGE and the CAS Application both built and ROOT Harness not, the Overview should also articulate the boundary between the CAS Application and SAGE (currently single-process but architecturally separate; future migration to two processes is explicit in CLAUDE.md). | Add subsection or extend existing one. |
| 3.5 | D | §6 Document Portfolio version numbers: every artifact version listed in the Overview is now stale. | Update versions to current state. |
| 3.6 | R | "Working Code" is now a substantial portfolio reality. The Overview's Document Portfolio section should acknowledge the code as a portfolio component alongside the documents. | Incorporate. |

### 4. ROOT Harness Architecture Reference (v1.0)

**Current section list:** Introduction; System Architecture; Orchestration Architecture; Agent Architecture; Validated Patterns; Deployment Model; Domain Instantiation Model; Formal Substrate Mapping; Open Questions.

The doc is forward-looking by definition (no code exists). Refresh is design-driven, not code-driven, but the document still benefits from reconciliation with what SAGE turned out to be and what we have learned about agent execution.

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.1 | S | "Sprint contract pattern for orchestrator plans" (tracker Open Considerations) — orchestrator plans must include explicit, negotiated success criteria before execution begins. Mechanism: workflow schema's `state_schema` enforces required fields; ROOT Harness refuses to execute workflows whose initial state does not satisfy the schema. | New subsection. |
| 4.2 | S | "Generator/evaluator separation pattern" (tracker Open Considerations) — GAN-inspired evaluator agents tuned for skepticism outperform generator self-assessment on finished work products. To be evaluated for PIM Health during ROOT Harness implementation. | New subsection or note in Validated Patterns. |
| 4.3 | S | "Context reset over compaction as default pattern" (tracker Open Considerations) — empirical evidence that context resets with structured handoff artifacts outperform in-context compaction. Orchestrators should be designed for clean handoffs through SAGE-mediated state. | New subsection. |
| 4.4 | R | The interrupt model and approval callback (CAS-ADR-013, also reflected in `interrupt.schema.json`) is at v1.0 but the rationale and the relationship to event-stream observability deserve substantive prose. | Incorporate. |
| 4.5 | D | §6 Deployment Model in the doc itself (separate from the Deployment Model document) may need adjustment if the chosen topology is one shared uvicorn process at first (matching SAGE+app today) vs. a separate process. The current deployment doc and tracker imply Phase 1 in-process; clarify here. | Verify; revise to match. |
| 4.6 | D | §9 Open Questions: items that have closed (steward agent model, decision logs, event stream, named ops, intentional ingestion) should be removed; items that remain should be carried forward. | Audit and revise. |

### 5. Deployment Model (v1.0)

**Current section list:** Introduction; Hardware; Operating System and Platform; Runtime Stack; SAGE Infrastructure; ROOT Harness Infrastructure; CAS Application; Development Environment; Directory Structure; Setup Procedures; Operational Notes.

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.1 | D | §5 SAGE Infrastructure: vault layout is `~/sage_vaults/{vault_id}/{vault_config.yaml, brain/{graph.db, lancedb/}, sources/}` with sources sometimes symlinked. Doc should state this explicitly. | Replace. |
| 5.2 | S | MCP server registration: stdio mode (older), SSE mode via `mcp-remote` bridge to FastAPI `/mcp/sse` (current default since 2026-04-14). Claude Desktop config path. | New subsection. |
| 5.3 | R | MLX model load behavior (Qwen3-30B-A3B-Instruct-2507-4bit, lazy on first abstract call, ~16-20 GB context once loaded), nomic-embed-text on CPU at max_seq_length=2048. The 64 GB envelope and the constraint that a second concurrent model is out of budget. | Incorporate. Cite CLAUDE.md as the operational source if helpful, but the doc should stand alone. |
| 5.4 | S | LanceDB compaction operational note from 2026-05-04 — vault-rewrite scripts must call `table.optimize()` to avoid fragment bloat; production `IngestionService.ingest()` does not compact, which will accumulate over months and warrants a maintenance task or per-N-ingests trigger (flagged for follow-up in tracker). | New subsection in Operational Notes. |
| 5.5 | D | §10 Setup Procedures: needs current pyproject.toml install commands (`pip install -e ".[test,mlx]"`), SAGE MCP server settings.json snippet, vault config bootstrap (`sage_create_vault` MCP tool delivered), test vault creation, vault paths. | Replace. |
| 5.6 | S | Stale-state pitfalls (LanceDB and SQLite stores live outside the repo; vault configs at `~/sage_vaults/{vault_id}/vault_config.yaml` survive `git reset`; MCP server holds stale imports until Claude Code restart; CLAUDE.md captures these) belong in Operational Notes for any non-Clif reader who needs to understand the local-Mac development model. | New subsection. |
| 5.7 | D | §6 ROOT Harness Infrastructure: stays prescriptive. Verify that the description matches the current ROOT Harness Architecture Reference design and is not a divergent forward declaration. | Verify; align. |
| 5.8 | D | §7 CAS Application: single-process React SPA + FastAPI uvicorn topology with static asset serving and `/app/*` URL prefix. | Replace. |

### 6. System Architecture Diagram (PNG, 2026-04-06)

| ID | Type | Item | Remediation |
|---|---|---|---|
| 6.1 | D | Predates catalog mode, sage_chain, ADR-017 edge model, ADR-018 intentional ingestion, ADR-021 caller-owned metadata, the MCP SSE mount, the multi-vault registry, the CAS Application backend topology. | Re-render. |
| 6.2 | S | No visual convention currently exists for distinguishing built from planned components. ROOT Harness is unbuilt; the diagram should convey this. | Add legend; use dashed outlines or a separate color for prescriptive components. |

Diagram refresh follows the textual docs; do not re-render until §1-§5 are stable.

### 7. Formatting Standards (v1.0)

**Current section list:** Purpose and Scope; File Naming Conventions; Page Setup; Typography; Paragraph Styles; Tables; Running Headers and Footers; Cover Page; Cross-References and Fields; Revision History Appendix; ADR Index Appendix; Programmatic Editing Conventions.

| ID | Type | Item | Remediation |
|---|---|---|---|
| 7.1 | N | No known drift. Document scope is stable. Verify during refresh pass that programmatic editing conventions still match how the .docx files have been edited; flag drift if found. | Verify; otherwise no-change. |

## Cross-document concerns

| ID | Item | Affects |
|---|---|---|
| C.1 | The "Working Code" portfolio component is now substantial and at beta-level production. Multiple reference documents implicitly assume code is in flight; updating each in isolation might leave seams. Recommend explicit cross-document language about implementation status as part of the refresh. | Overview, SAGE Arch, App Spec, ROOT Harness Arch, Deployment Model |
| C.2 | The supersedes chain is linear by design (architecture correction flagged in tracker §"Flagged for Discussion" 2026-04-08). This needs explicit statement somewhere — most naturally in SAGE Architecture Reference §4 — and a corresponding update to anywhere else that implies an exhaustive graph. | SAGE Arch (primary), ROOT Harness Arch (if it touches edge semantics) |
| C.3 | Domain Instantiation deliverable format: PIM Health is delivered as YAML configuration files at `domains/pim_health/*.yaml`, not as a Word document, per CAS-ADR-005 (independent document) and the format-follows-access-pattern principle. The Overview already acknowledges flexibility; verify other docs (especially ROOT Harness §7 Domain Instantiation Model) are consistent. | Overview, ROOT Harness Arch, Deployment Model, Formatting Standards |

## ADR debt (decisions found without ADRs)

Per session decision 2026-05-06: ADRs are reserved for substantive decisions whose preservation prevents drift from the architect's intent, not for routine implementation choices. Items below are candidates I encountered during the audit; final disposition is yours.

| ID | Decision | Recommendation |
|---|---|---|
| ADR.cand.1 | Sequential pipeline (Stages 1-3 complete before ingest returns) replacing prior async fire-and-forget. Driven by memory exhaustion on bulk ingest; load-bearing for the 64 GB envelope. | **Likely ADR-worthy.** Establishes a constraint on how SAGE behaves under load and is non-obvious from the API surface. Recommend draft. |
| ADR.cand.2 | Lazy MLX model loading deferred to first `generate_abstract()` call. | **Likely ADR-worthy** for the same reason as above; baseline RAM constraints are first-class architectural. |
| ADR.cand.3 | Embedding-on-CPU and max_seq_length=2048 for nomic-embed-text. | Borderline; could be a footnote in the Deployment Model or a separate ADR. Recommend Deployment Model footnote unless the constraint is later contested. |
| ADR.cand.4 | Single-process topology for SAGE Core API + CAS Application backend + MCP server (one uvicorn, three URL surfaces). Phase 1 design; explicit forward intent for separation. | **ADR-worthy.** A constraint on the topology that future contributors should not violate without deliberate revision. Recommend draft. |
| ADR.cand.5 | Linear supersedes chain (each version supersedes its immediate actual predecessor, not all prior versions). Flagged in tracker as architecture correction needed; not yet captured as a discrete decision in the ADR store. | **ADR-worthy.** Recommend draft. |
| ADR.cand.6 | Boundary-validation pattern: typed-alias validators on shape-bearing request-model fields applied uniformly so absence is itself the anomaly (2026-05-05). Establishes a discipline for future fields. | Likely ADR-worthy as a coding/contract discipline rather than as system architecture. Recommend draft if you want this preserved. |
| ADR.cand.7 | Pointer-direction principle clarification (session decision 2026-05-06): documents are self-contained narrative; ADRs serve as audit trail and conflict-resolution authority, not as the locus where substance lives. Refines the principle as currently stated in CLAUDE.md / CAS-ADR-008. | **ADR-worthy.** This is the constraint that governs the entire refresh pass. Recommend either a new ADR or a revision to CAS-ADR-008. |

## Open questions for Clif before edits begin

1. **Edit policy.** Versioned filename bump (e.g., `..._v2_0.docx`) for major refreshes, in-place overwrite with git as the version history, or per-document case-by-case? I recommend in-place with git plus a clear changelog entry in each document's revision history appendix; the versioned-filename convention has served well for milestone deliverables but doubles the bookkeeping for a portfolio-wide refresh.

2. **Sequence confirmation.** I propose: SAGE Arch, App Spec, Overview, Deployment Model, ROOT Harness Arch, Diagram, Formatting Standards. Is this the order you want, or do you want to lead with Overview to set terminology?

3. **ADR-debt threshold.** Items 1, 2, 4, 5, and 7 above are flagged as likely ADR-worthy. I'll draft them alongside the doc edits unless you want me to defer (or filter further).

4. **Version label inconsistency.** SAGE Architecture Reference filename is `_v1_4_2.docx`; tracker says v1.4. Confirm desired version label for the next revision.

## Process notes for the refresh pass

The plan, on approval of this inventory:

For each document, in the agreed sequence:
1. Re-read the .docx in full (extracted text already staged at `outputs/docx_extracts/`).
2. Walk the inventory items for that doc and confirm each is still accurate against the most recent code/tracker state.
3. Produce a redline (track-changes-style) plan or, where the rewrite is heavy enough, a fresh draft section.
4. Surface any new ADR candidates discovered during the rewrite.
5. After your approval of the section plan, apply the edits via the docx skill.
6. Run the mechanical conformance check on the saved .docx to verify it remains template-conformant.
7. Update the document's revision history and the project tracker.

The audit ends here. No .docx has been opened for edit.
