# CAS Application Spec v1.0 Drift Inventory

**Date:** 2026-05-08
**Author:** Clif (with Cowork assistance)
**Status:** Draft for review before any rewrite plan or .docx edits begin
**Predecessor under audit:** `docs/ref/2026-04-08_CAS_REF_App-Spec_v0_4.docx`
**Target version:** v1.0 (promotion from v0.4 to the first major release)

## Purpose

This inventory identifies the gap between (a) the CAS Application Spec v0.4 as authored on 2026-04-08 and (b) the as-built state of the CAS Application as of 2026-05-08. The application has been substantially built since v0.4: a Settings view that is not in the spec exists, the spec's separate Metadata Review and Edge Review sections are consolidated as a single Review view in code, and four edge types (`instantiated_from`, `references`, `retracts`, `merged_from`) added by CAS-ADR-017 are absent from the v0.4 graph and document-detail surfaces. Four ADRs (015 through 019) carry direct application-spec implications and post-date v0.4.

This is the audit deliverable that gates the v1.0 rewrite: no rewrite plan or prose drafting begins until this inventory is reviewed and accepted.

## Audit basis

| Source | State at audit time |
|---|---|
| App Spec v0.4 (.docx) | Issued 2026-04-08 |
| ADR store (`docs/cas_adr_store.json`) | 21 ADRs through ADR-021 (2026-05-01); ADRs 015 through 021 post-date v0.4 |
| Formal Substrate | `docs/fs/cas_app_api.openapi.yaml` (CAS App API at version 0.1.0); `docs/fs/sage/sage_core_api.openapi.yaml` (SAGE Core API consumed by the SPA); `docs/fs/sage/vault_config.schema.json` (the contract Settings reads and writes) |
| Built application | `app/src/views/*.tsx` (seven views: Dashboard, Ingest, Review, Search, DocumentDetail, GraphExplorer, Settings); `app/src/components/*.tsx` (Layout, Sidebar); `app/src/api/*.ts` (typed API client); `app/src/App.tsx` (route table) |
| SAGE Architecture Reference v2.0 | Issued 2026-05-08; referenced for retrieval modes, edge model, lifecycle state machine, and abstraction surface |
| Deployment Model v2.0 | Issued 2026-05-08; the App Spec's §3 Technology Stack and §4 Navigation Structure depend on Deployment Model v2.0's single-process topology |

ADRs that post-date v0.4 with material application-spec implications: ADR-015 (metadata extraction as a SAGE-level capability — affects ingest workflow), ADR-016 (UI-layer file metadata normalization — affects Dashboard health surface), ADR-017 (chain-scoped edge resolution — adds four edge types and anchor-field UI to Document Detail and Graph Explorer), ADR-018 (intentional ingestion only — confirms the absence of any auto-ingest UI), ADR-019 (mechanical-vs-curated provenance gate — affects Edge Review staging UX), ADR-021 (caller-owned metadata, parse-filename endpoint, needs_review per-call — affects Ingest workflow and Metadata Review). ADR-020 (abstraction prompt) is a SAGE-internal concern with no application-spec implication.

## Citation discipline

REF .docx files are authoritative for the architecture and application record; this inventory follows the same discipline. CLAUDE.md is reserved for steering Claude and is not cited as a source. As-built application facts surface during this audit are documented in the eventual v1.0 on the REF doc's own authority, ascertained by direct inspection of the application source code, the Formal Substrate, the ADR store, and the SAGE Architecture Reference. Specific ADRs are cited inline by ID where the decision is load-bearing for the prose; the cas_adr_store.json is the authoritative index of CAS ADRs and the v1.0 will not include an ADR-Index appendix (per the convention established by SAGE Architecture Reference v2.0 and Deployment Model v2.0).

## Scope discipline

The App Spec describes the CAS Application: its views, workflows, API consumption surfaces, and interaction patterns. The user's choice of browser is out of scope; specific visual-design decisions (layout, color palette, spacing) remain out of scope per v0.4's stated discipline. The frontend's npm-driven build pipeline and choice of React, react-router-dom, vis-network, and any test framework are CAS-side build decisions and stay in scope. Detailed implementation-internal decisions (component hierarchy, state-management library, CSS approach) stay out of scope as in v0.4 — the spec describes what the application does, not how its code is organized.

## Classification key

For each item, drift is classified as:

- **D — Drift.** The spec claims X; reality is Y. Resolution: rewrite to reflect reality.
- **R — Missing rationale.** The decision is reflected in operation but the why-it-is-so was captured only in an ADR or in the source code. Resolution: incorporate into the prose with a citation when an ADR is the source; document on the REF doc's own authority when the source is the running application.
- **S — Missing structure.** A view, workflow, or interaction pattern that a self-contained reader needs to understand the application but that v0.4 does not cover. Resolution: write a new section.
- **N — No drift.** Verified consistent with current state.

Remediation classifications: **replace**, **incorporate-with-citation**, **new-section**, **no-change**, **shrink-and-cite**.

## Summary

Overall severity: **high**. v0.4 was a specification produced before substantial implementation; the application now exists and many sections that previously specified intent now need to specify reality. The largest concentrations are at §4 (Navigation Structure, which omits Settings entirely), §6 (Ingest View, where ADR-021 caller-owned metadata and ADR-019 provenance gate land), §7-§8 (Metadata Review and Edge Review, which consolidate as a single Review section in built reality), §9 (Search, which has gained a fourth retrieval mode, pagination, and sortable columns), §10 (Document Detail, which now needs to surface eleven edge types and ADR-017 anchor fields), and one entirely new section: Settings.

| §  | Section | Currency | Principal drift |
|---|---|---|---|
| Cover | Title page | Stale-low | Date update; title formatting alignment with sister REF docs (optional) |
| 2 | Introduction | Mostly current | Cross-references and the ROOT-Harness placeholder framing remain valid; verify and refresh |
| 3 | Technology Stack | Stale-low | vis.js → vis-network terminology; cross-reference Deployment Model v2.0 single-process topology rather than re-describe; build tooling (vite, react-router-dom) optionally surfaced |
| 4 | Navigation Structure | Stale-medium | Five top-level views, not four (adds Settings); two drill-down views unchanged |
| 5 | Vault Dashboard | Mostly current | Verify all displayed statistics still present; ADR-016 UI-metadata normalization affects nothing user-visible at the Dashboard level (a quiet correctness improvement) |
| 5.4 | Adapter Registry | Verify | Confirm whether Adapter Registry remains a distinct Dashboard panel or moved to Settings → Source Adapters |
| 6 | Ingest View | Stale-medium | inferEdges toggle (new); ADR-021 needs_review per-call semantics; ADR-019 provenance gate visibility in Results Summary; status values 4 → 5 (adds adapter_disabled) |
| 7 | Metadata Review | Stale-high (structural) | Consolidates into Review view as a sub-tab; ADR-021 caller-owned metadata model affects the queue's population semantics |
| 8 | Edge Review | Stale-high (structural) | Consolidates into Review view as a sub-tab; ADR-019 mechanical-vs-curated provenance gate; ADR-017 introduces meta-edges (retracts, merged_from) into the staging surface |
| 9 | Search | Stale-medium | Four retrieval modes, not three (adds browse); pagination (50/page); sortable columns; pipeline_status filter (ADR-021 surface); document_date sort |
| 10 | Document Detail | Stale-high | Eleven edge types, not seven (per ADR-017: adds instantiated_from, references, retracts, merged_from); anchor-field requirements per resolution_policy (ADR-017); Manual Edge Creation dialog needs anchor-field UI |
| 11 | Graph Explorer | Stale-medium | Eleven edge types styled; meta-edges (retracts, merged_from) shown but visually distinct from semantic relationships; vis-network (not vis.js) terminology |
| New | Settings | Missing entirely | Seven-tab vault-configuration view, including vault creation: Identity, Document Types, Lifecycle, Source Adapters, Metadata Extraction, Edge Inference, Abstraction |
| App. A | Revision History | n/a | Append v1.0 entry; rename heading to "Appendix" (drop "A:") since this is the only appendix per the no-ADR-Index convention |

**New section expected:** §X Settings (seven tabs covering vault configuration).

**Sections to consolidate:** §7 Metadata Review and §8 Edge Review become one section, "Review," with subsections "Metadata" and "Edges."

## Per-section inventory

### Cover and §1 Table of Contents

| ID | Type | Item | Remediation |
|---|---|---|---|
| 2.0.1 | D | Cover date "April 8, 2026" → date of v1.0 finalization (2026-05-08). | Replace. |
| 2.0.2 | R (optional) | Subtitle "Architecture Reference Document". Sister REF docs use varying subtitle conventions (Deployment Model uses "Operational Reference Document"). The App Spec is genuinely architecture-and-specification rather than operations; the existing subtitle is appropriate. No change recommended unless cross-doc subtitle consistency is a goal. | Optional; default no-change. |
| 2.0.3 | n/a | TOC field auto-refreshes on Word open from current heading set. | No-change. |

### §2 Introduction

| ID | Type | Item | Remediation |
|---|---|---|---|
| 2.2.1 | N | "the primary tool through which a human operator manages vaults, ingests source documents, reviews inferred metadata and graph edges, searches the knowledge graph, and explores document relationships" — current; expand to also mention vault configuration once Settings is added. | Adjust to include vault configuration after §X Settings section is added. |
| 2.2.2 | N | "ROOT Harness interface sections (monitoring, agent management) will be added to this document as that subsystem matures." — current; ROOT Harness still unbuilt. | No-change. |
| 2.2.3 | N | "This specification describes functionality and user experience. It does not prescribe visual design (layout, color palette, spacing) beyond what is necessary to communicate interaction patterns." — preserves the visual-design-out-of-scope discipline. | No-change. |

### §3 Technology Stack

| ID | Type | Item | Remediation |
|---|---|---|---|
| 2.3.1 | D | "the application uses vis.js as a React component". The application uses **vis-network** (the renamed/scoped package). vis-network is the JS network-visualization library; vis.js is the umbrella name and outdated as a specific dependency reference. | Replace "vis.js" with "vis-network". |
| 2.3.2 | R | The single-process FastAPI topology described here duplicates content now canonically described in Deployment Model v2.0 §7 and §7.2. Cross-reference rather than re-describe. | Shrink and cite Deployment Model v2.0 for deployment topology; retain only the application-side build/runtime statement (React SPA, served as static assets, npm-driven build). |
| 2.3.3 | S (optional) | Build pipeline detail (vite, react-router-dom, test framework) is implementation-internal but a one-sentence summary of routing and state-management approach gives self-contained readers context for the route paths used in §4. | Optional incorporate. Light touch only. |
| 2.3.4 | N | "Node.js is required at build time but not at runtime." — confirmed by Deployment Model v2.0 §4.2; consistent. | No-change. |

### §4 Navigation Structure

| ID | Type | Item | Remediation |
|---|---|---|---|
| 2.4.1 | D | "four top-level views accessible from a persistent sidebar" → **five** top-level views. The current sidebar lists Dashboard, Ingest, Review, Search, and Settings. | Replace count and add Settings to the enumerated list. |
| 2.4.2 | D | The Review entry currently reads "presented as sub-tabs within a single view" — this is correct in built form (Metadata and Edges sub-tabs) but the spec's separate §7 and §8 sections imply otherwise. The §4 framing is ahead of §7-§8; the consolidation needs to land in §7-§8 as well. | Confirm; align §7-§8 to match. |
| 2.4.3 | S | Settings (new top-level) — describe in §4: "Vault configuration. Edit identity, document types, lifecycle, source adapters, metadata extraction, edge inference, and abstraction settings for the active vault. Also the entry point for creating a new vault from the sidebar." | New content. |
| 2.4.4 | N | "vault selector appears in the sidebar"; "Switching vaults resets the current view to the Dashboard." — verify in current sidebar behavior; appears intact. | No-change pending verification. |
| 2.4.5 | N | Document Detail and Graph Explorer remain as drill-down views reached by navigation rather than top-level entries. | No-change. |

### §5 Vault Dashboard

| ID | Type | Item | Remediation |
|---|---|---|---|
| 2.5.1 | N | §5.1 Vault Identity (name, description, base path) — verify still rendered; appears current. | No-change pending verification. |
| 2.5.2 | R | §5.2 Vault Statistics. The list enumerates: document count (total), documents by lifecycle state, documents by doc_type, documents by source adapter, edge count (total), edge count by type, staging edge count, LanceDB size, SQLite size, last ingestion timestamp. Verify each is still present. The "Edge count by type" bullet enumerates seven edge types (supersedes, covers, derived_from, bundles_with, authoritative_for, depends_on, sync_target); ADR-017 added four (instantiated_from, references, retracts, merged_from), so the breakdown is now eleven types. | Update edge-type enumeration to eleven types; verify other stats. |
| 2.5.3 | N | §5.3 Health Indicators (pending metadata review, pending edge review, deferred abstracts, failed ingestions) — current. | No-change. |
| 2.5.4 | D (verify) | §5.4 Adapter Registry. The Settings view has a Source Adapters tab (verifiable in `app/src/views/Settings.tsx`). Adapter management may have moved to Settings; verify whether the Dashboard still shows a separate Adapter Registry panel or whether this section should be removed in favor of cross-referencing Settings → Source Adapters. | Verify and either retain or remove. |

### §6 Ingest View

| ID | Type | Item | Remediation |
|---|---|---|---|
| 2.6.1 | N | §6.1 Step 1 Directory Input: directory path entry, validation, scan trigger. Current. | No-change. |
| 2.6.2 | D | §6.2 Step 2 Scan Preview status values: spec lists four (new, modified, unchanged, no adapter). The OpenAPI for `app_scan_directory` defines five (adds `adapter_disabled`: extension matches a vault adapter that has been disabled in the vault config). | Replace status enumeration with five values. |
| 2.6.3 | S | inferEdges toggle. The Ingest workflow now includes a per-batch toggle to enable or disable edge inference at ingestion time (visible in `Ingest.tsx`). v0.4 does not mention this toggle; the spec implies edge inference always runs as part of the pipeline. | New content; document the toggle and its default. |
| 2.6.4 | R | ADR-019 mechanical-vs-curated provenance gate affects the per-file ingest result surface: chain-identity-group repairs that touch hand-curated edges downgrade to Tier 2 staging and surface to Edge Review with the conflicting hand-curated edge alongside the proposed new edge. The Results Summary should describe this in terms a reader can use to interpret the per-batch outcome. | Incorporate-with-citation (ADR-019) in §6.4 Results Summary. |
| 2.6.5 | R | ADR-021 needs_review per-call semantics. The CAS Application bulk-ingest workflow passes `needs_review=true` so that the Metadata Review queue is populated for every UI-driven ingest (per ADR-021 Cleanup Phase A). v0.4 does not articulate this caller-side responsibility. The spec should note that bulk ingest from the UI populates the Metadata Review queue by design; agent-driven ingests (out-of-band) bypass the queue when the caller has authoritative metadata. | Incorporate-with-citation (ADR-021) in §6.4 Results Summary or in a §6 introductory paragraph. |
| 2.6.6 | N | §6.3 Step 3 Ingestion Progress: per-file progress, stage indication, scrolling log, cancel control. Current; SSE-streamed per `cas_app_api.openapi.yaml`. | No-change. |
| 2.6.7 | D | §6.4 Step 4 Results Summary. Edge inference results breakdown ("Tier 1 auto-created" vs "Tier 2 staged for review") preserved but per ADR-019 the partition of work between auto and staging now depends on the provenance gate. The summary should reflect provenance-gated outcomes (per chain-identity-group). | Replace breakdown framing per ADR-019. |

### §7 Metadata Review and §8 Edge Review (consolidated to "Review")

| ID | Type | Item | Remediation |
|---|---|---|---|
| 2.7.1 | D (structural) | v0.4 §7 and §8 are separate top-level sections. Built form: a single Review view with two sub-tabs (Metadata and Edges) selected via the `?tab=` URL parameter. v0.4's intuition that they live together is correct in §4's framing; the spec body has not yet been refactored to match. | Replace §7 and §8 with a single section "Review" containing two subsections: "Metadata" and "Edges." Renumber subsequent sections. |
| 2.7.2 | R | §7.1/§7.2 Metadata Review queue. ADR-021's caller-owned metadata model means the queue is populated only when the caller passes `needs_review=true`. The CAS Application's bulk-ingest workflow does so explicitly. The "winning value (per the content-overrides-filename precedence rule) highlighted" framing is from the pre-ADR-021 era when SAGE itself ran filename inference; under ADR-021 the FilenameParser is exposed via a SAGE endpoint that the application calls before commit, and the precedence rule is caller-side (CAS App: parse_filename → suggest → human confirm → call sage_ingest with confirmed metadata). The queue still surfaces fields with their extraction sources, but the "filename vs content vs default" mental model is now a UI suggestion artifact, not a SAGE-side conflict. | Incorporate-with-citation (ADR-021). Rewrite §7.1 to describe the post-ADR-021 review semantics. |
| 2.7.3 | R | §8 Edge Review. ADR-019 introduces the provenance gate: chain-identity groups whose repair touches only mechanically-inferred edges (rationale prefix `[version_chain]`) auto-execute as Tier 1 (auto unlink + link); groups containing any hand-curated edge downgrade to Tier 2 staging and present the entire repair atomically to the reviewer. The spec should describe how the staging surface presents these grouped repairs and the conflicting hand-curated edge alongside the proposed new edge. | Incorporate-with-citation (ADR-019). |
| 2.7.4 | D | §8 Edge Review groupings: spec lists "covers, derived_from, bundles_with" as Tier 2 groupings. ADR-017 changes the edge-type taxonomy: per-vault edge-type registry now declares resolution policy per type, and the Tier-2 partition is determined by the provenance gate per chain-identity group, not by edge type. The spec's groupings need to be reframed against the registry. | Replace groupings framing per ADR-017 + ADR-019. |
| 2.7.5 | R | ADR-017 meta-edges (retracts, merged_from) appear in the staging surface when their creation is staged rather than auto-applied. Document how the Edge Review surface handles meta-edges distinct from semantic-relationship edges. | Incorporate-with-citation (ADR-017). |

### §9 Search

| ID | Type | Item | Remediation |
|---|---|---|---|
| 2.9.1 | D | §9.2 Retrieval Modes: spec lists three (semantic, keyword, hybrid). Built has **four**: semantic, keyword, hybrid, **browse**. The browse mode is the catalog-like mode added with the SAGE Architecture Reference v2.0 retrieval-mode expansion (see SAGE Arch v2.0 retrieval architecture). | Replace mode enumeration with four. Describe browse mode's role (filter-driven catalog browsing without query text). |
| 2.9.2 | S | Pagination. Built has page-size 50 with offset/limit URL state. v0.4 does not address pagination. | New content; describe page size, navigation, and URL state. |
| 2.9.3 | S | Sortable columns. Built has sortable columns: title, doc_type, document_date, lifecycle_status, with asc/desc direction state in the URL. v0.4 does not address column sort. | New content. |
| 2.9.4 | D | §9.3 Filters. Spec lists doc_type, lifecycle state, and "metadata fields" generically. Built filters include: doc_type, lifecycle_status, project, pipeline_status. The pipeline_status filter is new (per ADR-021 pipeline_status surface; documents whose pipeline has not reached a terminal state can be filtered or excluded). | Replace filter enumeration with the four built filters. |
| 2.9.5 | R | URL state. Search filters, query, mode, sort, and offset are all URL-state parameters; this means the Search view is bookmarkable and shareable. v0.4 does not surface this. | Incorporate. |
| 2.9.6 | N | §9.1 Query Interface: text input, mode selector, filters, search button. Current. | No-change. |
| 2.9.7 | N | §9.4 Results Display: title, doc_type, lifecycle state, relevance score, snippet with query terms highlighted, abstract below snippet. Current; abstract presence governed by the abstraction stage outcome. | No-change pending verification. |

### §10 Document Detail

| ID | Type | Item | Remediation |
|---|---|---|---|
| 2.10.1 | N | §10.1 Metadata Panel (Tier 1 core; Tier 2 vault-configured; Tier 3 source-type-specific). Provenance fields. Current. | No-change. |
| 2.10.2 | N | §10.2 Projection Preview with heading hierarchy intact; "Open Source File" button. Current. | No-change. |
| 2.10.3 | D | §10.3 Edge List currently implies seven edge types. Per ADR-017, the edge type taxonomy is now eleven: supersedes, derived_from, instantiated_from, covers, references, bundles_with, depends_on, authoritative_for, sync_target, retracts, merged_from. The Edge List section should describe the full set and note the meta-edge category (retracts, merged_from) distinctly from semantic-relationship edges. | Replace edge-type enumeration. |
| 2.10.4 | R | ADR-017 anchor fields and resolution policies. Edges now carry source_valid_from_version and target_valid_from_version anchor fields, governed by per-edge-type resolution policy (transitive_source, transitive_target, transitive_both, none). The Edge List entry for any edge should display anchor information when set; the Manual Edge Creation dialog must collect anchor fields where the edge type's resolution policy requires them. | Incorporate-with-citation (ADR-017). |
| 2.10.5 | D | §10.4 Manual Edge Creation. v0.4 says "Tier 3 edges (authoritative_for, depends_on, sync_target) and any other edges the user wishes to create manually". Under ADR-017 the registry per-edge-type drives behavior, not Tier classification per edge type. The dialog needs to collect resolution-policy-appropriate anchor fields and to optionally accept an explicit `retracts` target (a one-sided source-anchored edge that targets another edge). The Tier-3-as-edge-type-list framing is stale. | Replace per ADR-017's registry-driven model. |
| 2.10.6 | R | ADR-021 caller-supplied metadata. Manual edits to metadata fields in Document Detail's Metadata Panel pass through the parse-filename suggestion path or the metadata update path with the `metadata_confirmed=true` semantics (the human edit is authoritative, overriding both filename and content extraction). | Incorporate-with-citation (ADR-021). |

### §11 Graph Explorer

| ID | Type | Item | Remediation |
|---|---|---|---|
| 2.11.1 | D | §11.1 Visualization. v0.4 describes generic visual encoding (node shape per doc_type, edge dash pattern + color per edge type, node opacity per lifecycle state). Built form has settled mappings (visible in `app/src/views/GraphExplorer.tsx`): doc_type shapes (patent_draft → diamond, technical_disclosure → box, reference → ellipse, status_report → triangle, meeting_notes → star, note → dot, article → square, bookmark → triangleDown), and per-edge-type dash pattern + color for all eleven edge types. The spec has held back from committing to specific mappings as "implementation decisions." Decision: commit the mappings (locking cross-vault visual consistency), or leave as implementation choices and only enumerate the encoding *channels*? The latter has been the v0.4 discipline. | Open question: commit or stay agnostic? See Open Questions. |
| 2.11.2 | D | "vis.js library is imported directly into the frontend build" → vis-network. | Replace. |
| 2.11.3 | R | ADR-017 meta-edges (retracts, merged_from) shown but visually distinct from semantic relationships (per `GraphExplorer.tsx`'s edge-style mapping). The visualization treats meta-edges as a category. | Incorporate-with-citation (ADR-017). |
| 2.11.4 | N | §11.2 Interaction Model (hover tooltip, click selects, double-click navigates, pan/zoom). Current. | No-change. |
| 2.11.5 | N | §11.3 Controls (traversal depth slider, edge-type filter checkboxes, lifecycle state filter checkboxes, layout toggle, re-center button). Current. | No-change. |

### §X Settings (NEW)

| ID | Type | Item | Remediation |
|---|---|---|---|
| 2.S.1 | S | Entire view missing from v0.4. The Settings view (`app/src/views/Settings.tsx`) is the seven-tab vault-configuration surface and the entry point for creating new vaults from the sidebar. The seven tabs: Identity (id, name, owner, storage_root, brain_root, visibility), Document Types (vault's doc_type enumeration with value, label, description per type), Lifecycle (states with terminal flag, transitions with from/to/action and optional creates_edge), Source Adapters (registered adapters with enabled flag), Metadata Extraction (filename rules per the vault_config schema), Edge Inference (per-vault edge inference config), Abstraction (per-vault abstraction config). | New section. The structure mirrors the seven configurable areas of `vault_config.schema.json`. |
| 2.S.2 | S | Vault creation. Initiated from the Sidebar's "New Vault" affordance. The user supplies id (matching `^[a-z][a-z0-9_-]*$`), name, and owner; the application constructs a default vault_config payload and posts to the SAGE Core API. The new vault's storage_root defaults to `~/sage_vaults/{vault_id}/sources` and brain_root to `~/sage_vaults/{vault_id}/brain`. After creation, the user is navigated into the new vault's Settings view to refine its configuration. | New content. Describe the create flow's preconditions, default config, and post-create navigation. |
| 2.S.3 | S | Read-modify-write semantics. The Settings view loads the active vault's config via `getVaultConfig`, allows editing, and writes back via `updateVaultConfig`. Validation is server-side against `vault_config.schema.json`; the UI surfaces validation errors per field. The application does not maintain a local-edit-then-save workflow distinct from the underlying schema; what the user sees is the schema's structure with a UI per tab. | New content. |
| 2.S.4 | R | ADR-018 confirms ingestion is exclusively intentional; the Settings view does not surface any "auto-ingest" toggle because none exists. | Incorporate-with-citation (ADR-018). |

### Appendix A: Revision History → Appendix

| ID | Type | Item | Remediation |
|---|---|---|---|
| 2.A.1 | D | Heading "Appendix A: Revision History" → "Appendix" (drop "A:"). Per the no-ADR-Index convention established by SAGE Architecture Reference v2.0 and Deployment Model v2.0, the App Spec v1.0 will not have an ADR Index appendix; with only one appendix, the suffix-letter is unnecessary. (Compare Deployment Model v2.0 which has the same single-appendix shape.) | Rename heading. |
| 2.A.2 | n/a | Append a v1.0 entry in the existing prose format (`v1.0 (date):` bold prefix, BodyText paragraph). Existing entries v0.1 through v0.4 untouched. | Append. |

## New section recommendations

The drift inventory surfaces one major new section, one significant consolidation, and one rename:

1. **§X Settings** — new section, seven tabs covering vault configuration plus the vault-creation flow. Place between the existing §10 Document Detail and §11 Graph Explorer? Or before §5 Vault Dashboard (since it covers the vault's identity and configuration rather than its operational surface)? Or after §11 Graph Explorer (preserving the existing §5-§11 narrative arc and adding Settings as the most-configurable view at the end)? The rewrite plan should pick one; my recommendation is after §11 Graph Explorer to preserve the operational arc, but a case can be made for early placement.

2. **Consolidate §7 Metadata Review and §8 Edge Review into a single section "Review"** with two subsections "Metadata" and "Edges." Renumber subsequent sections accordingly.

3. **Rename §12 Appendix A: Revision History → Appendix** per the no-ADR-Index single-appendix convention.

The rewrite plan should sequence these structural changes early so subsequent prose work operates against the final section structure.

## Open questions for the rewrite plan

1. **Visual encoding commitment.** §11 Graph Explorer's doc_type → shape mapping and edge-type → dash-pattern-and-color mapping are settled in code but described as implementation decisions in v0.4. Should v1.0 commit them (locking cross-vault visual consistency, requiring spec updates for any future change) or stay agnostic? My recommendation: commit. The mappings have been stable, are visible in code, and shape-doc_type-mapping consistency across vaults is a usability win for any reader who works across multiple vaults. Doc_type shape mapping can be characterized as "a vault may extend the mapping for its custom doc_types" so vaults retain flexibility.

2. **Build pipeline detail.** §3 Technology Stack currently names React, vis.js, and FastAPI. Should v1.0 also name react-router-dom (route handling), vite (build), and the test framework? Mirror the level of detail used in Deployment Model v2.0 §4.1 Python (which names venv, pip, editable-install, extras). My recommendation: light additions (route library, build tool) but skip test-framework detail unless setup-relevant. The Deployment Model is the right place for build-tool runtime details; the App Spec just needs enough that a reader can map what they see to the underlying stack.

3. **Settings detail level.** Should the Settings section enumerate every field in every tab (high-fidelity, brittle to vault_config.yaml changes) or describe each tab's purpose with reference to the schema (lower-fidelity, more durable)? My recommendation: schema-anchored — name the tab, describe its purpose, name the configurable areas, refer the reader to the vault_config.schema.json (in the Formal Substrate) for the authoritative field list. The schema is the live source; the spec describes the UI surface that consumes it.

4. **CAS Application backend API surface.** The spec's §3 currently describes the application as consuming "the SAGE Core API." With ADR-021 cleanup Phase A, the application also consumes the CAS Application API (the `/app/*` endpoints documented in `cas_app_api.openapi.yaml`). Should §3 enumerate both surfaces? My recommendation: yes; both APIs are CAS-side, both are documented in the Formal Substrate, and a reader of the App Spec who is trying to understand what API surface the SPA exercises needs both.

5. **Vault-creation flow placement.** Vault creation is initiated from the Sidebar but completes via the Settings view (the new vault is then editable in Settings). Should this flow be described in §X Settings (as I currently recommend), in §4 Navigation Structure (as a sidebar affordance), or in a separate vault-management section? My recommendation: §X Settings, with a forward reference from §4 Navigation Structure. Vault creation is fundamentally a configuration act; Settings is the right home.

6. **Eventual cas vault provisioning.** CAS-ADR-022 (proposed) anticipates the CAS reference docs moving into a `cas` SAGE vault. The App Spec's relationship to this is identical to other REF docs and does not require special accommodation. No App-Spec-specific item.

## Closing

This inventory enumerates drift across 12 sections of v0.4 plus identifies one missing section (Settings) and one structural consolidation (Metadata Review + Edge Review → Review). Severity is high overall: the application is built; v0.4 was a pre-implementation specification; ADRs 015 through 019 plus 021 land application-spec implications that have not been incorporated. The pre-existing documentation in v0.4 is structurally sound, however, and the rewrite is mostly a section-by-section update against built reality and ADR ratifications rather than a from-scratch rewrite. The standards established in the SAGE Arch v2.0 and Deployment Model v2.0 rounds (self-contained narrative, no historical framing in the body, citation discipline, scope discipline, no ADR-Index appendix) apply uniformly. Target version is v1.0 (promotion from pre-1.0).

End of inventory.
