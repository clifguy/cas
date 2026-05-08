# ROOT Harness Architecture Reference v2.0 Drift Inventory

**Date:** 2026-05-08
**Author:** Clif (with Cowork assistance)
**Status:** Draft for review before any rewrite plan or .docx edits begin
**Predecessor under audit:** `docs/ref/2026-03-31_CAS_REF_ROOT-Harness-Architecture_v1_0.docx`
**Target version:** v2.0 (promotion paralleling SAGE Architecture Reference v2.0, Deployment Model v2.0, App Spec v1.0, Overview v2.0, and Formatting Standards v2.0)

## Purpose

This inventory identifies the gap between (a) the ROOT Harness Architecture Reference v1.0 as authored on 2026-03-31 and (b) the conventions and architectural state of CAS as of 2026-05-08. ROOT Harness remains unbuilt; the rewrite is design-driven rather than as-built, and the principal work is to apply the now-codified authoring conventions (Formatting Standards v2.0 §12) and align cross-references to the rest of the v2.0 REF set.

The principal drift falls into five categories: (1) several v1.0 sections include narrative framings that compare current design to absent prior states — the "v0.1 gatekeeper pattern" reference, the "v0.1 four-value enum," and similar — which the codified no-historical-framing-in-body rule excludes; (2) §2.4 and §6.4 describe MCP stdio as the Phase 1 default, while the post-Deployment-Model-v2.0 convention is SSE-mounted-at-/mcp with standalone-stdio as fallback; (3) §7.4 cross-references specific SAGE Architecture Reference subsection numbers (§3.4.6, §3.4.7) which are stale under SAGE Arch v2.0's renumbered structure and would land more durably as concept references; (4) Appendix A is the ADR Index that the codified no-ADR-Index convention removes; (5) Appendix B becomes the sole appendix and renames to "Appendix" per the single-appendix-letter convention.

## Audit basis

| Source | State at audit time |
|---|---|
| ROOT Harness Architecture Reference v1.0 (.docx) | Issued 2026-03-31 |
| ADR store (`docs/cas_adr_store.json`) | 21 ADRs through ADR-021; ROOT-Harness-relevant ADRs include 001 (LangGraph), 003 (naming), 005, 006, 007, 008, 009, 010 (steward agent model, two-type taxonomy), 012 (decision logs), 013 (typed event stream); subsequent ADRs (014-021) primarily affect SAGE and have only tangential ROOT Harness implications |
| Formal Substrate | `docs/fs/root_harness/orchestration_api.openapi.yaml`, `docs/fs/root_harness/event_stream.schema.json`, `docs/fs/root_harness/interrupt.schema.json`, `docs/fs/root_harness/policy.schema.json`; substrate v1.14 |
| Formatting Standards (current) | Codifies citation discipline, scope discipline, no-historical-framing-in-body, self-contained narrative, no-ADR-Index, single-appendix naming, and inter-document references using generic names |
| CAS Overview (current) | Codifies Single source of truth, Pointer direction, and Architectural-vs-deployment distinction as Design Principles 9, 10, and 11 |
| Sister REF docs (current) | SAGE Architecture Reference, Deployment Model, CAS Application Spec — all under the v2.0 standards; Deployment Model establishes the SSE-mounted-at-/mcp Phase 1 MCP transport convention |

## Citation discipline

REF .docx files are authoritative. CLAUDE.md is reserved for steering Claude and is not cited as a source. Specific document versions are not cited inline. Specific ADRs are cited inline by ID where load-bearing; the cas_adr_store.json is the authoritative index.

## Scope discipline

ROOT Harness Architecture describes the orchestration component of CAS — its layers, agents, workflows, deployment, and instantiation model. The user's choice of MCP client, browser, or developer tooling is out of scope. Domain-specific content belongs in the relevant instantiation document. ROOT Harness is unbuilt; design intent is in scope, but speculative implementation detail beyond what is architecturally committed is not.

## Classification key

- **D — Drift.** The doc claims X; reality (or codified convention) is Y.
- **R — Missing rationale.** A current convention is reflected but the rationale lives only in an ADR or in the Formatting Standards / Overview principles.
- **S — Missing structure.** A new section needed.
- **N — No drift.** Verified consistent.

Remediation classifications: **replace**, **incorporate-with-citation**, **new-section**, **no-change**, **remove**.

## Summary

Overall severity: **medium**. ROOT Harness Arch v1.0 is structurally sound and substantively current at the architectural level. The concentrated drift is in (a) historical framing references that the codified no-historical-framing-in-body rule excludes, (b) MCP transport alignment, and (c) Appendix A removal / Appendix B rename.

| §  | Section | Currency | Principal drift |
|---|---|---|---|
| Cover | Title page | Stale-low | Date update |
| 1.1 | Scope | Mostly current | Verify; minor refresh if needed |
| 1.2 | Relationship to SAGE | Current | No-change |
| 2.1 | Architectural Layers | Current | No-change |
| 2.2 | Boundary Rule | Current | No-change; CAS-ADR-010 framing remains accurate |
| 2.3 | Agent Registration Model | Stale-low | Remove the "Replaces the v0.1 enum of autonomous, gatekeeper, mechanical, interactive" phrase from the agent_type description (historical framing in body) |
| 2.4 | MCP Integration Points | Stale-medium | Phase 1 framing should mirror SAGE/Deployment Model: SSE-mounted-at-/mcp as primary, standalone-stdio as fallback |
| 2.5 | Orchestration API Surface | Current | No-change; verify against current orchestration_api.openapi.yaml |
| 2.6 | Event Stream | Current | No-change |
| 3.1 | LangGraph Selection Rationale | Current | The options-considered framing is design-time architectural rationale, not historical comparison; pointer to CAS-ADR-001 for fuller evaluation is the right pattern |
| 3.2-3.5 | Orchestration Architecture rest | Current | No-change |
| 4.1 | Agent Registration and Identity | Current | No-change |
| 4.2 | Steward Agent Model | Stale-low | Remove "This subsumes the v0.1 gatekeeper pattern" sentence (historical framing in body); the steward contract description is otherwise current |
| 4.3 | Behavioral Constraints and Policy Enforcement | Current | No-change |
| 4.4 | Steward Mutation Governance | Stale-low | Remove "This pattern was described in v0.1 as the standalone gatekeeper agent; CAS-ADR-010 absorbs it into the steward contract" sentence (historical framing in body) |
| 4.5 | Failure Modes and Recovery Strategies | Current | No-change |
| 5 | Validated Patterns | Current | No-change |
| 6 | Deployment Model | Stale-low | §6.4 Phase 1 transport row should mirror the SSE-mounted convention; minor cross-reference language alignment |
| 7.1-7.3 | Domain Instantiation Model (early subsections) | Current | No-change |
| 7.4 | Metadata Extraction and Edge Inference Configuration | Stale-low | Cross-reference to "SAGE Architecture Reference Sections 3.4.6 and 3.4.7" is brittle; replace with concept names |
| 7.5-7.6 | Domain Instantiation Model (late subsections) | Current | No-change |
| 8 | Formal Substrate Mapping | Current | No-change; verify against current substrate |
| 9 | Open Questions | Mostly current | Five items; minor refresh if any have moved toward resolution |
| App. A | CAS ADR Index | Remove | Per codified no-ADR-Index convention |
| App. B | Revision History | Rename | "Appendix B" → "Appendix"; append v2.0 entry |

## Per-section inventory

### Cover

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.0.1 | D | Cover date "March 31, 2026" → date of v2.0 finalization (2026-05-08). | Replace. |
| 5.0.2 | n/a | Subtitle "Architecture Reference Document" — appropriate; ROOT Harness Arch is genuinely architecture and specification. | No-change. |

### §1 Introduction

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.1.1 | N | "ROOT Harness (Runtime for Orchestration, Operations, and Testing) is the orchestration component of CAS (Clif's Agentic System)." Current. | No-change. |
| 5.1.2 | N | "For portfolio identity, goals, and design principles, see the CAS Overview." Generic cross-reference; no version label. Aligned with the codified inter-document-reference rule. | No-change. |
| 5.1.3 | N | "The name carries three metaphors simultaneously" (test harness, wiring harness, working harness; CAS-ADR-003). Current. | No-change. |

### §1.1 Scope

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.1.4 | N | The bullet lists of what ROOT Harness is and is not responsible for. Current. | No-change. |
| 5.1.5 | N | "Observability: execution traces, decision logs, and performance metrics via LangSmith." LangSmith is a runtime dependency (named tool), not a developer tool, so naming it is in scope. | No-change. |

### §1.2 Relationship to SAGE

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.1.6 | N | Boundary rule framing and the state-vs-execution categorical question test. Current. | No-change. |

### §2 System Architecture

#### §2.1 Architectural Layers

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.2.1 | N | Four-layer model (Orchestration API, Workflow Engine, Agent Layer, Integration Layer). Current. | No-change. |

#### §2.2 Boundary Rule

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.2.2 | N | Two-tier rule per CAS-ADR-010. Current. | No-change. |

#### §2.3 Agent Registration Model

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.2.3 | D | The agent_type field description: "Classification: steward, orchestrator (CAS-ADR-010). Replaces the v0.1 enum of autonomous, gatekeeper, mechanical, interactive." The "Replaces the v0.1 enum..." phrase is historical framing in the body and is excluded by the codified no-historical-framing-in-body rule. The architectural fact (the two-type taxonomy via CAS-ADR-010) is captured; the comparison to a prior enum belongs in the revision history. | Replace with the description ending after the CAS-ADR-010 citation. |

#### §2.4 MCP Integration Points

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.2.4 | D | "In Phase 1 (local, single machine), MCP transport uses stdio." The post-Deployment-Model-v2.0 convention is that Phase 1 prefers FastMCP SSE mounted at /mcp on the FastAPI server, with standalone-stdio as fallback. ROOT Harness is unbuilt, so this is design-intent alignment rather than as-built reframing. The eventual ROOT Harness MCP transport should mirror the SAGE pattern for the same reasons (sharing connections, model loads, in-process state across callers rather than duplicating per client process). | Replace. |

#### §2.5 Orchestration API Surface

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.2.5 | N | Nine operations table (trigger_workflow, get_status, approve, list_pending, get_agent_history, register_agent, get_agent, get_pipeline_status, subscribe_events). Verify against the current orchestration_api.openapi.yaml; v1.0 was already brought into substrate conformance. | No-change pending verification. |

#### §2.6 Event Stream

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.2.6 | N | Typed event stream per CAS-ADR-013. Seven-category event taxonomy. Current. | No-change. |

### §3 Orchestration Architecture

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.3.1 | N | §3.1 LangGraph Selection Rationale: the options-considered framing (LangGraph, CrewAI, AutoGen, custom asyncio) is design-time architectural rationale, not narrative comparison to a prior state of CAS itself. The pointer to CAS-ADR-001 for the full evaluation is the right pattern under self-contained-narrative + pointer-direction. | No-change. |
| 5.3.2 | N | §3.2 Graph Structure (sequential, conditional, parallel fan-out and merge). Current. | No-change. |
| 5.3.3 | N | §3.3 Checkpointing and Durable Execution. Current. | No-change. |
| 5.3.4 | N | §3.4 Human-in-the-Loop Interrupt Pattern, including cancellation. Current per v1.0's substrate-conformance update. | No-change. |
| 5.3.5 | N | §3.5 SAGE Core API Interaction Patterns table. Current; the table reflects the post-CAS-ADR-014 named operations (set_lifecycle, update_metadata) and the CAS-ADR-010 steward-mediated access model. | No-change. |

### §4 Agent Architecture

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.4.1 | N | §4.1 Agent Registration and Identity. Current. | No-change. |
| 5.4.2 | D | §4.2 Steward Agent Model. The steward contract (sole authority, mediated access, governed mutation, efficient retrieval) is current. The phrase "This subsumes the v0.1 gatekeeper pattern" in the governed-mutation bullet is historical framing in the body. | Replace by removing the trailing sentence; the architectural fact stands without the comparison to the prior pattern. |
| 5.4.3 | D | §4.4 Steward Mutation Governance: "This pattern was described in v0.1 as the standalone gatekeeper agent; CAS-ADR-010 absorbs it into the steward contract because the four governance responsibilities are inherent to any agent that owns a canonical artifact." The first clause is historical framing in the body; the second clause is the architectural rationale and should remain. | Rewrite to drop the historical comparison while preserving the architectural rationale. Proposed: "Mutation governance comprises four responsibilities inherent to any agent that owns a canonical artifact (per CAS-ADR-010)." |
| 5.4.4 | N | §4.3 Behavioral Constraints and Policy Enforcement, §4.5 Failure Modes and Recovery Strategies. Current. | No-change. |

### §5 Validated Patterns

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.5.1 | N | All six patterns (Session Replacement by Persistent Agent State, Execution Mechanism Taxonomy, Interactive Scope Boundary, Boundary Rule as Pattern, Steward Agent Pattern, Planning-Then-Execution Handoff). Current; the patterns hold up well as transferable architectural insights. | No-change. |

### §6 Deployment Model

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.6.1 | D | §6.4 Transport Requirements by Phase. Phase 1 row shows "MCP stdio" for AI client → ROOT Harness. Per the Deployment Model's SSE-mounted-at-/mcp convention, Phase 1 should prefer FastMCP SSE on the integrated FastAPI server, with standalone-stdio as fallback. The framing should mirror the established convention so the eventual ROOT Harness MCP transport inherits the CAS-portfolio pattern. | Replace Phase 1 cell. |
| 5.6.2 | N | §6.1 Phase 1 Local Single-Machine, §6.2 Phase 2 Hybrid, §6.3 Phase 3 Mature Multi-Tenant. Forward-looking design narrative; current. | No-change. |
| 5.6.3 | N | §6 cross-reference: "Concrete environment details (hardware, tool versions, paths, setup procedures) are documented separately in the Deployment Model document (CAS-ADR-006)." Generic cross-reference; aligned with the inter-document-reference rule. | No-change. |

### §7 Domain Instantiation Model

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.7.1 | N | §7.1-§7.3, §7.5-§7.6. Current. | No-change. |
| 5.7.2 | D | §7.4 Metadata Extraction and Edge Inference Configuration: "See SAGE Architecture Reference Sections 3.4.6 (Metadata Extraction) and 3.4.7 (Edge Inference) for the configuration schema and tier model." The specific section numbers are stale under SAGE Arch v2.0's reorganized structure and are brittle in any case. The codified inter-document-reference rule prefers concept references over section numbers. | Replace with: "See the SAGE Architecture Reference for the metadata extraction and edge inference architecture, including the configuration schema and tier model." |

### §8 Formal Substrate Mapping

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.8.1 | N | §8.1-§8.4. ROOT Harness contributions to the substrate: Orchestration API, agent registration schema, pipeline configuration schema, policy configuration schema, workflow graph schema, interrupt contracts, event stream schema, approval policy schema. Current; aligned with the substrate's root_harness/ subdirectory. | No-change. |

### §9 Open Questions

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.9.1 | N | Five items: workflow versioning, agent-to-agent communication (partially resolved per CAS-ADR-010), observability retention policy, concurrent workflow isolation, agent capability discovery. All remain forward-looking design considerations. | No-change. |

### Appendix A: CAS ADR Index — REMOVED

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.A.1 | D | Per the codified no-ADR-Index convention (Formatting Standards §11), Appendix A is removed entirely. The eleven ADRs it enumerates (CAS-ADR-001, 003, 005, 006, 007, 008, 009, 010, 012, 013) all appear inline in the body where their decisions are load-bearing for the prose; no information is lost by removing the appendix. | Remove. |

### Appendix B: Revision History → Appendix

| ID | Type | Item | Remediation |
|---|---|---|---|
| 5.B.1 | D | "Appendix B: Revision History" → "Appendix: Revision History" per the codified single-appendix-letter convention (Formatting Standards §10.1). | Rename. |
| 5.B.2 | n/a | Append a v2.0 entry. | Append. |

## New section recommendations

The drift inventory recommends no new sections. The structural changes are: Appendix A removal, Appendix B rename, and the targeted in-place edits enumerated above.

## Open questions for the rewrite plan

1. **MCP transport language alignment.** §2.4 and §6.4 both reference the Phase 1 MCP transport. The Deployment Model establishes SSE-mounted-at-/mcp as the Phase 1 default with standalone-stdio as fallback, and that convention extends to ROOT Harness's eventual MCP transport when ROOT Harness is built. Should the ROOT Harness Arch v2.0 commit to mirroring the SAGE pattern, or stay generic about transport ("MCP transport per the deployment model's conventions")? My recommendation: commit. The forward-compatibility-and-resource-sharing rationale that drove SAGE's choice (sharing model loads, database connections, and in-process state across callers rather than duplicating per client process) applies identically to ROOT Harness when it is built.

2. **Section number cross-references.** §7.4 currently cross-references SAGE Architecture Reference subsection numbers (§3.4.6, §3.4.7). The codified inter-document-reference rule prefers generic-name references; specific section numbers are brittle across revisions even within the same target document. Should v2.0 of ROOT Harness Arch use section-number cross-references at all, or only concept references? My recommendation: concept references only. If a reader needs a specific subsection in the target document, the target document's own TOC will surface it. Section-number citations across documents are a maintenance liability with little reader benefit.

3. **Open Questions section.** The five forward-looking items remain forward-looking. The "Agent-to-agent communication" item is "partially resolved per CAS-ADR-010"; the no-peer-calls rule is established as the starting position. Should this item be marked resolved (with the no-peer-calls rule recorded as the resolution) or kept as partially open? My recommendation: keep as partially open. The phrase "the question remains partially open for mature multi-agent workflows where collaborative patterns between stewards might emerge" is a fair statement of the current state; CAS-ADR-010 establishes the default architectural stance but does not foreclose future evolution.

4. **§8 Formal Substrate Mapping verification.** The eight-item list of ROOT Harness substrate contributions (§8.2) was authored against the substrate at the time of v1.0. The current substrate manifest may contain additions, renames, or removals; quick verification is in order before the rewrite to ensure the §8.2 list still reflects the substrate's actual ROOT Harness contributions. My recommendation: verify before drafting; substantive changes in §8.2 if any are found.

5. **§2.5 Orchestration API Surface verification.** Same verification concern: the nine-operation table should match the current orchestration_api.openapi.yaml. v1.0 was brought into conformance with substrate v0.2; post-substrate-v0.2 changes (if any) would need to be reflected. My recommendation: verify before drafting; light update if any operations have been added.

## Closing

This inventory enumerates drift across nine body sections plus appendices. Severity is medium overall, with the principal drift concentrated in three places: historical-framing references in §2.3, §4.2, and §4.4 (excluded by the codified no-historical-framing-in-body rule); MCP transport alignment in §2.4 and §6.4; and the standard removal of Appendix A and rename of Appendix B. ROOT Harness remains unbuilt, so most of v1.0's substantive design content holds up well; the rewrite is principally a series of small in-place edits plus the appendix changes. Standards from the prior REF doc rounds apply uniformly. Target version is v2.0.

End of inventory.
