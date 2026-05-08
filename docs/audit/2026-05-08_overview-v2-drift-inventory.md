# CAS Overview v2.0 Drift Inventory

**Date:** 2026-05-08
**Author:** Clif (with Cowork assistance)
**Status:** Draft for review before any rewrite plan or .docx edits begin
**Predecessor under audit:** `docs/ref/2026-03-30_CAS_REF_Overview_v1_3.docx`
**Target version:** v2.0 (promotion paralleling SAGE Architecture Reference v2.0 and Deployment Model v2.0)

## Purpose

This inventory identifies the gap between (a) the CAS Overview v1.3 as authored on 2026-03-30 and (b) the current state of the CAS portfolio as of 2026-05-08. The Overview is the document a new reader encounters first; aligning it with the standards established this round (self-contained narrative, no historical framing, citation discipline, scope discipline, no ADR-Index appendix) has high downstream value for any reader entering the portfolio.

The principal drift falls into four categories: (1) the explicit ADR-index-appendix prescription in §5 Ecosystem Artifacts directly contradicts the no-ADR-Index convention now operating across SAGE Arch v2.0, Deployment Model v2.0, and App Spec v1.0; (2) the §6 Document Portfolio table lists Formal Substrate and Test Plan as Planned when both are built; (3) the §4.4 CAS Application section uses "HTML5 web application" framing pre-App-Spec-v1.0 and does not reflect the as-built React SPA + FastAPI backend + integrated MCP architecture; (4) Appendix A is exactly the ADR Index that the new convention removes.

## Audit basis

| Source | State at audit time |
|---|---|
| Overview v1.3 (.docx) | Issued 2026-03-30 |
| ADR store (`docs/cas_adr_store.json`) | 21 ADRs through ADR-021 (2026-05-01); ADRs 011 through 021 post-date v1.3 |
| Formal Substrate (`docs/fs/manifest.json`) | substrate v1.14, 2026-05-02; built rather than Planned |
| Project Tracker (`CAS_Project_Tracker.md`) | v95, last updated 2026-05-08; reflects current portfolio status |
| SAGE Architecture Reference v2.0 | Issued 2026-05-08; the Overview's §4.1 SAGE description must align |
| Deployment Model v2.0 | Issued 2026-05-08; the Overview's §5 Deployment Model description must align |
| CAS Application Spec v1.0 | Issued 2026-05-08; the Overview's §4.4 must align with the as-built React SPA + FastAPI + MCP architecture |
| Built code (`sage/`, `app/`, `tests/`) | Confirms SAGE and CAS App are operational; ROOT Harness remains unbuilt |

ADRs that post-date v1.3 with material Overview-level implications: ADR-013 (typed event stream — affects ROOT Harness §4.2 framing), ADR-015 through ADR-019 and ADR-021 (substantive SAGE behavior changes that the §4.1 SAGE component description should reflect at one-line summary level), CAS-ADR-022 (proposed; the docs-as-SAGE-vault trajectory affects how the Overview frames the CAS ADR store and the eventual fate of `docs/ref/`), CAS-ADR-023 (proposed; linear supersedes chain — minor relevance to §5 framing).

## Citation discipline

REF .docx files are authoritative for the architecture and portfolio record; this inventory follows the same discipline. CLAUDE.md is reserved for steering Claude and is not cited as a source. Architectural facts surface during this audit are documented in the eventual v2.0 on the REF doc's own authority, ascertained by direct inspection of the ADR store, the Formal Substrate, the project tracker, and the sister REF docs (SAGE Arch v2.0, Deployment Model v2.0, App Spec v1.0). Specific ADRs are cited inline where load-bearing; the cas_adr_store.json is the authoritative index of CAS ADRs and the v2.0 will not include an ADR-Index appendix.

## Scope discipline

The Overview describes CAS — its identity, goals, design principles, components, ecosystem artifacts, and document portfolio. The user's choice of browser, MCP client, or developer tooling is out of scope per the convention established in Deployment Model v2.0 §8 and applied uniformly across the REF series. Domain-specific content belongs in the relevant instantiation document, not in the Overview; the Overview names the planned instantiations but does not describe them in detail.

## Classification key

- **D — Drift.** The doc claims X; reality is Y. Resolution: rewrite to reflect reality.
- **R — Missing rationale.** The decision is reflected in operation but the why-it-is-so was captured only in an ADR. Resolution: incorporate into the prose with an inline ADR citation.
- **S — Missing structure.** A portfolio-level concept that a self-contained reader needs but that v1.3 does not cover. Resolution: write a new section or paragraph.
- **N — No drift.** Verified consistent with current state.

Remediation classifications: **replace**, **incorporate-with-citation**, **new-section**, **no-change**, **shrink-and-cite**.

## Summary

Overall severity: **medium**. The Overview's structure is sound and most of its content holds up well; the principal drift is concentrated in §5 (the CAS ADR store entry's appendix-prescription), §6 (Document Portfolio table statuses and missing entries), §4.4 (CAS Application as-built architecture), and the removal of Appendix A entirely. Design Principles need a small expansion to absorb principles that have crystallized since v1.3; §4.1 SAGE component description benefits from a one-line refresh acknowledging post-v1.3 ADR work; §4.5 Domain Instantiations is largely current.

| §  | Section | Currency | Principal drift |
|---|---|---|---|
| Cover | Title page | Stale-low | Date update |
| 1 | Introduction | Mostly current | Verify, refresh CAS Application reference |
| 2 | Goals | Current | No-change |
| 3 | Design Principles | Stale-low | Consider absorbing single-source-of-truth and pointer-direction principles that have crystallized since v1.3 |
| 4.1 | SAGE | Mostly current | Add one-line acknowledgment of post-v1.3 ADR maturation (chain-scoped edges, intentional ingestion, caller-owned metadata) |
| 4.2 | ROOT Harness | Current | No-change; remains unbuilt |
| 4.3 | Boundary Rule | Current | No-change |
| 4.4 | CAS Application | Stale-medium | Reframe from "HTML5 web application" to as-built React SPA + FastAPI backend + integrated MCP transport; cite App Spec v1.0 |
| 4.5 | Domain Instantiations | Mostly current | Verify Resurrection scope framing; no other drift |
| 5 | Ecosystem Artifacts | Stale-medium | CAS ADR store entry rewritten per the no-ADR-Index convention; mention CAS-ADR-022 trajectory |
| 6 | Document Portfolio and Reading Guide | Stale-medium | Table statuses (Formal Substrate, Test Plan are built, not Planned); add CAS Application Spec; verify Formatting Standards inclusion; possibly add the Formatting Standards row |
| App. A | CAS ADR Index | Remove | Remove entirely per the no-ADR-Index convention |
| App. B | Revision History | n/a | Rename heading from "Appendix B" to "Appendix"; append v2.0 entry |

## Per-section inventory

### Cover

| ID | Type | Item | Remediation |
|---|---|---|---|
| 3.0.1 | D | Cover date "March 30, 2026" → date of v2.0 finalization (2026-05-08). | Replace. |
| 3.0.2 | n/a | Subtitle "Operational Reference Document". The Overview is more architectural than operational, but the subtitle is shared with Deployment Model v2.0; no compelling reason to diverge. | No-change. |

### §1 Introduction

| ID | Type | Item | Remediation |
|---|---|---|---|
| 3.1.1 | N | "CAS (Clif's Agentic System) is a personal experimental system for building, operating, and learning from an agentic ecosystem. It encompasses three core components: a memory system (SAGE), an orchestration system (ROOT Harness), and a human-facing application (CAS Application)." Verifiably current. | No-change. |
| 3.1.2 | N | "CAS is not a product, a framework, or a startup." Current; the framing remains accurate. | No-change. |
| 3.1.3 | N | "The name is deliberately personal." Current. | No-change. |

### §2 Goals

| ID | Type | Item | Remediation |
|---|---|---|---|
| 3.2.1 | N | Four goals (Solve the AI memory problem; Experiment with building an agentic ecosystem on local hardware; Impose rigor on a vibe-coding exercise; Gain durable, transferable learning). All four still apply. | No-change. |

### §3 Design Principles

| ID | Type | Item | Remediation |
|---|---|---|---|
| 3.3.1 | N | The eight principles in v1.3 (Local-first; Domain-neutral; Rigorous experimentation; Transferable learning; Dark factory orientation; Steward-mediated access; Orchestrator-mediated coordination; Format follows access pattern). Each remains in force. | No-change. |
| 3.3.2 | S (optional) | Two principles have crystallized in the architectural discourse since v1.3 and may warrant promotion to the canonical Design Principles list: **Single source of truth** (every concept has one authoritative home; documents do not duplicate authoritative data; the no-ADR-Index convention is one application) and **Pointer direction** (architecture documents point to the Formal Substrate and ADR store; neither points back to documents; documents are self-contained narrative, ADRs are audit trail and conflict-resolution authority). Both have been operating implicitly across the SAGE Arch v2.0, Deployment Model v2.0, and App Spec v1.0 work. | Optional incorporate; see Open Questions for the rewrite plan. |
| 3.3.3 | S (optional) | A third candidate principle: **Architectural-vs-deployment distinction** (an architectural decision establishes a constraint or structure that governs the system across deployments; a deployment-environment constraint is driven by the specific resources of a particular deployment). This was the principle that drove the SAGE Arch v2.0 / Deployment Model v2.0 content split. Could land as a stand-alone principle or as a clarifying paragraph under the Format-follows-access-pattern principle. | Optional incorporate; see Open Questions. |
| 3.3.4 | n/a | Implementation-specific conventions (e.g., "Pydantic models derived from schemas") that appear in CLAUDE.md should not be promoted to the Design Principles list — they're conventions, not principles, and belong in Formatting Standards or component-specific REF docs. | No-change. |

### §4 System Components

#### §4.1 SAGE

| ID | Type | Item | Remediation |
|---|---|---|---|
| 3.4.1 | N | "SAGE is the memory component of CAS. It provides structured, persistent memory for AI-assisted workflows by combining semantic search, a typed relationship graph, lifecycle state management, and multi-format source ingestion into a unified architecture accessed through a protocol-neutral API." Holds up well as an Overview-level summary. | No-change. |
| 3.4.2 | N | "SAGE owns document state: identity, metadata, relationships, lifecycle, access control, and provenance. It indexes everything and owns nothing — source files have a life independent of SAGE, and the system tracks rather than absorbs them." Current. | No-change. |
| 3.4.3 | R (optional) | The post-v1.3 ADR work (chain-scoped edges per ADR-017, intentional-only ingestion per ADR-018, mechanical-vs-curated provenance gate per ADR-019, caller-owned metadata per ADR-021) has matured SAGE materially since v1.3 was authored. The Overview-level paragraphs remain accurate but a one-line acknowledgment that "SAGE has matured through ADRs 011 through 021 since v1.3 was authored" would be redundant in a self-contained narrative; the cross-reference to "the SAGE Architecture Reference for the full specification" already directs the reader to the current authoritative source. | No-change recommended (the current pointer is sufficient). |

#### §4.2 ROOT Harness

| ID | Type | Item | Remediation |
|---|---|---|---|
| 3.4.4 | N | "ROOT Harness is the orchestration component of CAS." Forward-looking statement; ROOT Harness remains unbuilt. Steward agent model (CAS-ADR-010), two-type taxonomy, LangGraph (CAS-ADR-001) all current. | No-change. |

#### §4.3 Boundary Rule

| ID | Type | Item | Remediation |
|---|---|---|---|
| 3.4.5 | N | The two-tier rule (steward agents call SAGE Core API; orchestrators access via stewards) is current. The closing paragraph's three diagnostic questions ("Is this about what the data says?" / "Is this about what should happen next?" / "Is this about a specific artifact's content or state?") still apply. | No-change. |

#### §4.4 CAS Application

| ID | Type | Item | Remediation |
|---|---|---|---|
| 3.4.6 | D | "It is an HTML5 web application that provides the interface for browsing SAGE-managed documents..." → reframe per App Spec v1.0: a React single-page application served as static assets by the same Python uvicorn process that hosts the SAGE Core API and the CAS Application backend, with an integrated FastMCP SSE transport at /mcp. The single-process FastAPI topology is the as-built deployment shape. | Replace. Cite App Spec v1.0 and Deployment Model v2.0 for full detail. |
| 3.4.7 | D | "The application consumes both the SAGE Core API (for document search, metadata, relationships, and lifecycle state) and the ROOT Harness Orchestration API..." → three API surfaces, not two: the SAGE Core API directly, the CAS Application backend at /app/* (covering scan, batch ingest with SSE-streamed progress, and the parse-filename endpoint), and the ROOT Harness Orchestration API (forward-looking; ROOT Harness remains unbuilt). | Replace. |
| 3.4.8 | N | "It is a CAS component, not a development tool." — current. The scope-discipline distinction between CAS components and developer tools holds up. | No-change. |

#### §4.5 Domain Instantiations

| ID | Type | Item | Remediation |
|---|---|---|---|
| 3.4.9 | N | "A domain instantiation is a consumer of SAGE and ROOT Harness, not a component of either." — current. | No-change. |
| 3.4.10 | N | Format-follows-access-pattern note for instantiation deliverables (YAML configuration directories where appropriate; Word documents for primarily-prose contexts). Current. | No-change. |
| 3.4.11 | D (verify) | Planned instantiations list: PIM Health (current); Resurrection ("IT Department management"); college basketball ("Analytics and scouting data management"); Personal ("General-purpose knowledge management"). The Resurrection scope framing — "IT Department management" — may be narrower than the eventual instantiation will be (Resurrection United Methodist Church's IT-led work has organizational scope beyond strict IT-Department-only operations). Verify with the rewrite plan whether to keep, broaden, or leave as-is. | Verify and adjust if needed. |

### §5 Ecosystem Artifacts

| ID | Type | Item | Remediation |
|---|---|---|---|
| 3.5.1 | D | CAS ADR store entry: "Documents carry an CAS ADR index appendix with pointers, not full CAS ADR content." This prescriptive statement directly contradicts the no-ADR-Index convention now operating: SAGE Arch v2.0, Deployment Model v2.0, and App Spec v1.0 do not have ADR Index appendices. The CAS ADR store entry should be rewritten to reflect the current convention: reference documents cite specific ADRs inline by ID where the decision is load-bearing for the prose; the cas_adr_store.json is the authoritative, machine-consumable, never-stale index of CAS architecture decisions, with full options-considered, rationale, and consequences for each ADR. | Replace. |
| 3.5.2 | R (optional) | CAS-ADR-022 (proposed): the trajectory toward maintaining CAS reference documentation as a SAGE vault, with version history captured by supersedes edges in the vault's graph store rather than by filename versioning, is worth foreshadowing in the Overview at one-sentence depth. The current `docs/ref/` placement is transitional. | Incorporate-with-citation (CAS-ADR-022). Brief footnote-level mention. |
| 3.5.3 | N | Formal substrate entry: "Architecture documents describe what gets specified and why; the formal substrate contains the specifications themselves." Current; the substrate is built (manifest v1.14) and the framing holds. | No-change. |
| 3.5.4 | N | Deployment model entry: "An operational artifact capturing concrete environment details." Current. | No-change. |
| 3.5.5 | N | Test plan entry: "Contract-driven test specifications derived from the formal substrate. Each formal specification becomes a testable assertion. The test plan is the bridge between architecture and running code." Current. | No-change. |
| 3.5.6 | N | Working code entry. Current. | No-change. |

### §6 Document Portfolio and Reading Guide

| ID | Type | Item | Remediation |
|---|---|---|---|
| 3.6.1 | D | Document Portfolio table statuses: Formal Substrate listed as Planned. The Formal Substrate is built (manifest v1.14, 2026-05-02). Update status to Development (the convention used elsewhere in the table for built-but-evolving artifacts). | Replace status. |
| 3.6.2 | D | Test Plan listed as Planned. Per the project tracker, Test Plan is at v0.9.19 with substantial coverage across SAGE tier 1, tier 2 behavioral, adapters, provenance, app, MCP, edge inference, and many other areas. Update status to Development. | Replace status. |
| 3.6.3 | S | CAS Application Spec missing from the table. Add a row: scope "CAS application: views, workflows, API consumption, settings"; status Development; reading order something like "After SAGE Architecture Reference / before Deployment Model" or "Reference for application work". | New row. |
| 3.6.4 | S | Formatting Standards missing from the table. Add a row: scope "Reference document formatting and conventions"; status Development; reading order "Reference". | New row. |
| 3.6.5 | N | Reading-order paragraph after the table ("The CAS Overview assumes no prior context...") remains accurate; pattern carries forward to v2.0 with adjustments for the added rows. | Adjust to reflect added rows. |
| 3.6.6 | D | PIM Health Instance row: status "Planned". Per the project tracker, PIM Health Instance is at v0.1 (Development). Update status. | Replace status. |

### Appendix A: CAS ADR Index — REMOVED

| ID | Type | Item | Remediation |
|---|---|---|---|
| 3.A.1 | D | Per the no-ADR-Index convention established by SAGE Architecture Reference v2.0 and Deployment Model v2.0 and applied to App Spec v1.0, the entire Appendix A is removed. The cas_adr_store.json is the authoritative index; specific ADRs are cited inline in the body of the Overview where their decisions are load-bearing for the prose. The current Appendix A's seven entries (CAS-ADR-004, 005, 007, 008, 009, 010, 012) all appear inline in the v1.3 body or are subsumed by the §5 Ecosystem Artifacts CAS ADR store entry; no information is lost by removing the appendix. | Remove entire Appendix A. |

### Appendix B: Revision History → Appendix

| ID | Type | Item | Remediation |
|---|---|---|---|
| 3.B.1 | D | With Appendix A removed, the sole remaining appendix is correspondingly renamed from "Appendix B: Revision History" to "Appendix: Revision History" per the single-appendix-letter convention used in Deployment Model v2.0 and App Spec v1.0. | Rename heading. |
| 3.B.2 | n/a | Append a v2.0 entry in the established prose format (`v2.0 (date):` bold prefix, BodyText paragraph). Existing entries v0.1 through v1.3 untouched. | Append. |

## New section recommendations

The drift inventory does not require any structurally new sections in the body. Two optional additions are flagged in the Open Questions: whether to extend Design Principles with one or two principles that have crystallized since v1.3, and whether to add a brief CAS-ADR-022 forward-looking note in §5.

The structural changes are:
- §6 Document Portfolio table grows by two rows (CAS Application Spec, Formatting Standards) and updates three status cells (Formal Substrate, Test Plan, PIM Health Instance).
- Appendix A is removed entirely.
- Appendix B is renamed to Appendix.

## Open questions for the rewrite plan

1. **Design Principles extension.** Two principles have crystallized in operation since v1.3 was authored: Single source of truth (every concept has one authoritative home; the no-ADR-Index convention is one application; the cas_adr_store.json is authoritative for ADRs and not duplicated in REF docs) and Pointer direction (architecture documents point to the Formal Substrate and ADR store; neither points back to documents; documents are self-contained narrative). Should v2.0 add either or both as ninth and tenth principles? My recommendation: yes to both. They are operating principles that have already shaped the v2.0 round of REF doc revisions and naming them in the Overview makes them durable for future REF revisions. The Single-source-of-truth principle subsumes the pattern that dropped the ADR-Index appendix from REF docs; Pointer direction is the structural rule that determines which way cross-references point. A third candidate (Architectural-vs-deployment distinction) is also worth considering as either a stand-alone eleventh principle or a clarifying note under Format-follows-access-pattern; my recommendation: as a stand-alone principle, since it played a load-bearing role in the SAGE Arch v2.0 / Deployment Model v2.0 content split.

2. **CAS-ADR-022 forward-looking note in §5.** Should the Overview foreshadow the docs-as-SAGE-vault trajectory, similar to the way Deployment Model v2.0 §9 does? My recommendation: yes; one sentence under the CAS ADR store entry or under Working code is sufficient to alert a reader to the impending change without committing to specifics that depend on ratification.

3. **Resurrection instantiation framing.** "IT Department management" may be narrower than the eventual scope. The rewrite plan should ask Clif whether to keep the framing, broaden it (for example, "operational management for Resurrection United Methodist Church's IT-led portfolio" or similar), or leave it deliberately narrow until the instantiation matures and the actual scope is clear. My recommendation: leave it narrow; broaden when the instantiation begins and concrete scope emerges.

4. **Document Portfolio table addition: Formatting Standards.** The Formatting Standards document exists (v1.0, 2026-03-30) and is in the project tracker. Should it appear in the Overview's portfolio table? My recommendation: yes, with status Development and reading order Reference. Including it makes the portfolio table comprehensive and gives readers a complete map.

5. **Reading-order narrative.** With the table growing by two rows, the prose paragraph after the table will need a one-sentence adjustment: where do CAS Application Spec and Formatting Standards fall in the reading order? My recommendation: CAS Application Spec is "After ROOT Harness Architecture Reference / before Deployment Model" for someone who needs to understand the application surface; Formatting Standards is Reference. Both belong in the prose after the table.

6. **CAS-ADR-022 + ADR JSON principle landing.** You mentioned earlier that the CAS Overview revision should pick up the principle that the cas_adr_store.json is the authoritative ongoing source of ADRs. Item 3.5.1 above lands this principle in the §5 CAS ADR store entry rewrite. The principle does not need a separate dedicated section; surfacing it in the §5 entry where the ADR store is described is the natural home.

## Closing

This inventory enumerates drift across six body sections plus appendices. Severity is medium overall, concentrated at §5 (the appendix-prescription contradiction with the new convention), §6 (statuses and missing entries), §4.4 (CAS Application as-built reframing), and the removal of Appendix A. The Overview's structure is sound; the rewrite is principally a series of in-place updates plus the removal of one appendix and the rename of the other. Standards from the prior REF doc rounds (self-contained narrative, no historical framing in the body, citation discipline, scope discipline, no ADR-Index appendix) apply uniformly. Target version is v2.0.

End of inventory.
