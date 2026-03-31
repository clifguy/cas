# CAS Project Tracker

**Last updated:** 2026-03-31

---

## Portfolio Artifacts

| # | Artifact | Current Version | Status | Next Version |
|---|----------|----------------|--------|--------------|
| 1 | CAS Overview | v1.3 | Development | — |
| 2 | SAGE Architecture Reference | v1.4 | Development | — |
| 3 | ROOT Harness Architecture Reference | v1.0 | Development | — |
| 4 | Deployment Model | v1.0 | Development | — |
| 5 | ADR Store | v1.10 (schema), 14 ADRs | Development | — |
| 6 | Formatting Standards | v1.0 | Development | — |
| 7 | Formal Substrate | v1.0 | Development | — |
| 8 | Test Plan | — | Planned | v0.1 |
| 9 | Working Code | — | Planned | — |

## Domain Instantiation Documents

| # | Artifact | Current Version | Status | Next Version |
|---|----------|----------------|--------|--------------|
| 10 | PIM Health Instance | v0.1 | Development | — |
| 11 | Resurrection Instance | — | Planned | v0.1 |
| 12 | College Basketball Instance | — | Not started | — |
| 13 | Personal Instance | — | Not started | — |

---

## Pending Work by Artifact

### 1. CAS Overview (v1.3)

- Promoted to v1.0 on 2026-03-25. Added CAS Application to System Components. Updated introduction to reference three core components. All sections have complete coverage with no known gaps.
- v1.1 delivered 2026-03-26. CAS ADR disambiguation: replaced bare "ADR" references with "CAS ADR" and "CAS-ADR-NNN" throughout.
- v1.3 delivered 2026-03-30. Instantiation deliverable format flexibility. Updated Domain Instantiations subsection (Section 4.5) to acknowledge that deliverables may be YAML configuration directories rather than Word documents, following the format-follows-access-pattern principle.
- No further pending corrections or revisions identified.

### 2. SAGE Architecture Reference (v1.4)

- v1.1 delivered 2026-03-26. CAS ADR disambiguation: replaced bare "ADR" references with "CAS ADR" and "CAS-ADR-NNN" throughout.
- v1.2 delivered 2026-03-30. Semantic abstract and retrieval health monitoring (CAS-ADR-011).
- v1.3 delivered 2026-03-30. Named Core API operations (CAS-ADR-014). Decomposed organize() into set_lifecycle() and update_metadata() in the Core API Operations table (Section 7). Forcing function: policy enforcement requires gating by operation name, not parameter inspection. Added CAS-ADR-014 to the ADR Index.
- v1.4 delivered 2026-03-30. Formal Substrate v1.0 conformance. Added get_document(document_id) to the Core API Operations table (Section 7). Returns the full document record including all metadata tiers, lifecycle status, and pipeline status. Required by the formal substrate's OpenAPI specification: the async ingest pattern (endpoint returns immediately after projection) needs a polling mechanism for callers to check indexing and abstraction progress.
- No further pending corrections or revisions identified.

### 3. ROOT Harness Architecture Reference (v1.0)

- v0.3 delivered 2026-03-26. CAS ADR disambiguation: replaced bare "ADR" references with "CAS ADR" and "CAS-ADR-NNN" throughout.
- v0.4 delivered 2026-03-30. Decision log companion artifacts (CAS-ADR-012).
- v0.5 delivered 2026-03-30. Typed event stream and tool-level approval callback (CAS-ADR-013). Added subscribe_events to the Orchestration API table and new Event Stream subsection (Section 2.6). Added approval callback paragraph to Steward Mutation Governance (Section 4.4) and implementation mechanism paragraph to Execution Mechanism Taxonomy (Section 5.2). Added event stream schema and approval policy schema to formal substrate contributions (Section 8.2).
- v1.0 delivered 2026-03-31. Formal Substrate v1.0 conformance. (1) Added get_agent operation to the Orchestration API Surface table (Section 2.5); the OpenAPI specification defines this as a GET endpoint for retrieving a single agent's registration record, a natural REST companion to register_agent that was absent from the table. (2) Added workflow cancellation paragraph to the Human-in-the-Loop Interrupt Pattern section (Section 3.4), describing cancelled as a terminal execution status distinct from failure and rejection, with rationale for why cancellation leaves no orphaned document state. Both items were identified during conformance review of the Formal Substrate v1.0 API contracts against this document. Promoted to v1.0: all sections have complete coverage with no known gaps.
- No further pending corrections or revisions identified.

### 4. Deployment Model (v1.0)

- v0.2 delivered 2026-03-26. CAS ADR disambiguation: replaced bare "ADR" references with "CAS ADR" and "CAS-ADR-NNN" throughout.
- v1.0 delivered 2026-03-31. Claude Code transition and directory structure update. (1) Revised Section 9 (Directory Structure) to reflect the established repository layout: added docs/fs/ (Formal Substrate), docs/ref/ (architecture reference documents), and domains/ (domain instantiation configurations) as named rows. Updated the ~/repos/cas/ row to document the CLAUDE.md project memory file and project tracker at the repository root. Updated the docs/ row description to include the CAS ADR store, CPML specification, and subdirectory structure. (2) Updated the post-table guidance paragraph to describe the expanded docs/ and new domains/ directories. (3) Added CLAUDE.md setup instructions to Section 10.6 (Development Tools). (4) Added CLAUDE.local.md to the .gitignore exclusion list in Section 10.1. (5) Reframed Section 11.4 (Troubleshooting) from placeholder to living-document format. Promoted to v1.0: all sections have complete coverage with no known gaps.
- No further pending corrections or revisions identified.

### 5. ADR Store (v1.10)

- Cleaned up on 2026-03-25: all consequences rewritten to state architectural impacts only, removing document-section prescriptions. Schema version bumped from 1.5 to 1.6.
- CAS-ADR-012 added 2026-03-30: Decision logs as SAGE-managed steward and orchestrator companion artifacts.
- CAS-ADR-013 added 2026-03-30: Typed event stream for agent execution observability.
- CAS-ADR-014 added 2026-03-30: Named Core API operations replacing organize(). Decomposes organize() into set_lifecycle() and update_metadata() for direct policy enforcement by operation name.
- Schema version bumped from 1.9 to 1.10 on 2026-03-31.
- No further pending work identified.

### 6. Formatting Standards (v1.0)

- v0.1 delivered 2026-03-25. Covers file naming, page setup, typography, paragraph styles, tables, headers/footers, cover page, cross-reference fields, revision history and ADR index appendix formats, and programmatic editing conventions.
- v0.2 delivered 2026-03-25. Added Section Numbering subsection documenting multilevel list numbering linked to heading styles.
- v0.2.1 delivered 2026-03-25. CAS ADR disambiguation: updated ADR references to use "CAS ADR" prefix convention.
- v1.0 delivered 2026-03-30. Instantiation deliverable format flexibility. Updated INST type code description and post-table guidance to acknowledge that deliverables may be YAML configuration directories. Promoted to v1.0: all sections have complete coverage with no known gaps.
- No further pending corrections or revisions identified.

### 7. Formal Substrate (v1.0)

- Independent CAS artifact per CAS-ADR-008. Directory of structured JSON Schema files and OpenAPI specifications at docs/fs/, organized by component (sage/, root_harness/) with a manifest.json at the root.
- v0.1 delivered 2026-03-30. Ten configuration schemas across SAGE (6) and ROOT Harness (4), covering all twelve items in the ROOT Harness instantiation checklist. JSON Schema draft 2020-12. Individual files per format-follows-access-pattern principle.
- v0.1 updated 2026-03-30. Schema review and manifest cleanup: removed organize from policy.schema.json permitted_operations enum (CAS-ADR-014). Manifest: removed errant $schema/$id keywords (manifest is a data document, not a schema), added decision log entry schema to deferred list (CAS-ADR-012), removed source_references from all schema and deferred entries (architecture documents point to the substrate, not the reverse; provenance recorded in revision history only).
- v1.0 delivered 2026-03-31. API contracts and data model schemas resolving all five deferred items from v0.1: (1) SAGE Core API OpenAPI specification (sage/sage_core_api.openapi.yaml), 14 operations across 7 tags, developed against SAGE Architecture Reference v1.4 Section 7. (2) ROOT Harness Orchestration API OpenAPI specification (root_harness/orchestration_api.openapi.yaml), 9 operations across 5 tags, developed against ROOT Harness Architecture Reference v1.0 Section 2.5. (3) Event stream schema (root_harness/event_stream.schema.json), 15 event types in 7 categories per CAS-ADR-013. (4) Interrupt contracts and approval policy schema (root_harness/interrupt.schema.json), covering InterruptDescriptor, ApproveRequest, ApprovalPolicy, and supporting types. (5) Decision log schema (sage/decision_log.schema.json), three entry categories per CAS-ADR-012. Promoted to v1.0: all contributions listed in SAGE Architecture Reference Section 8.3 and ROOT Harness Architecture Reference Section 8.2 are present with no known gaps.
- No further pending corrections or revisions identified.

### 8. Test Plan (Planned)

- Contract-driven test specifications derived from the formal substrate. Each formal specification becomes a testable assertion.
- Depends on: Formal Substrate v1.0 (delivered).

### 9. Working Code (Planned)

- Implementation of SAGE, ROOT Harness, and domain instantiation configurations.
- Depends on: Formal Substrate (delivered), Test Plan. (Git setup completed 2026-03-26.)

### 10. PIM Health Instance (v0.1)

- Independent domain instantiation per CAS-ADR-005. The deliverable is the YAML configuration files themselves, not a separate prose document. Well-commented YAML serves both consumers (humans reading domain setup, code loading configuration at runtime) from a single source of truth, following the same format-follows-access-pattern reasoning that made the Formal Substrate a directory of JSON Schema files.
- First and most complex domain instantiation.
- v0.1 delivered 2026-03-30. Five YAML configuration files validated against FS v0.1 schemas: sage_vault_config.yaml (checklist items #1-#6), pipeline.yaml (#7, #10), agents.yaml (#8), policies.yaml (#9), workflows.yaml (#11, #12). Target path: ~/repos/cas/domains/pim_health/. Committed and pushed to GitHub.
- YAML files updated 2026-03-30: removed organize from permitted_operations in three policies (CAS-ADR-014), added set_lifecycle to reference_steward_read_heavy, corrected target paths from root_harness/ subdirectory to flat domain directory.
- No further pending corrections or revisions identified.

### 11. Resurrection Instance (Planned)

- IT Department management domain instantiation.
- Scope and timeline to be determined.

### 12. College Basketball Instance (Not started)

- Analytics and scouting data management domain instantiation.
- No work begun.

### 13. Personal Instance (Not started)

- General-purpose knowledge management domain instantiation.
- No work begun.

---

## Open Considerations

- **Generator/evaluator separation pattern.** GAN-inspired multi-agent pattern using separated generator and evaluator agents for artifact quality gates. Externally validated by Anthropic Labs (Rajasekaran, Mar 2026) and LangChain (Trivedy, Mar 2026). Evaluator agents tuned for skepticism outperform generator self-assessment on finished work products. Evaluate for PIM Health domain during ROOT Harness implementation.
- **Sprint contract pattern for orchestrator plans.** Orchestrator plans must include explicit, negotiated success criteria before execution begins. Derived from Anthropic Labs harness work where generator and evaluator negotiated a "sprint contract" defining testable completion criteria before implementation. Mechanism: the workflow schema's state_schema (docs/fs/root_harness/workflow.schema.json) defines required fields per workflow, including success criteria. Specific plan-shape constraints are domain configuration, defined per workflow in each domain instantiation. ROOT Harness enforces the requirement by refusing to execute workflows whose initial state does not satisfy the state_schema.
- **Context reset over compaction as default pattern.** Empirical evidence (Anthropic Labs) that context resets with structured handoff artifacts outperform in-context compaction for long-running agent work. LangGraph checkpoint-and-resume already supports this pattern. Orchestrators should be designed for clean handoffs through SAGE-mediated state, not in-context summarization.

---

## Flagged for Discussion

- **Self-evaluation scope.** Steward self-review of plans and checklists appears sound (validated by Clif's experience across hundreds of PIM sessions). Independent evaluation may be needed for finished artifacts, where the producing agent has accumulated context-level commitment to its own decisions. The failure mode is strongest when evaluating effort-intensive completed work, not lightweight pre-execution plans. Needs further analysis to determine where CAS draws the line.
- **Formal Substrate as plan-shape authority (partially resolved).** The Formal Substrate defines the mechanism for valid plan shape: the workflow schema's state_schema section specifies required fields, types, and constraints for each workflow's state dictionary. ROOT Harness enforces plan validity at execution time by validating initial state against the state_schema before dispatching. The separation-of-concerns pattern holds: Formal Substrate says "plans must have this shape," ROOT Harness says "I won't run plans that don't have this shape." Remaining: the specific plan-shape constraints (which fields constitute a valid plan, what success criteria look like) are defined per domain instantiation. PIM Health will be the first test of whether the mechanism is expressive enough.

---

## Outstanding Cross-Cutting Items

- **Git setup.** Completed 2026-03-26. Git 2.50.1 installed on Mac Mini. Repository root: ~/repos/. SGC repository initialized, pushed to private GitHub remote (clifguy/sgc) with SSH authentication (ed25519 key). ZSH aliases configured (sgc, cas). Git setup procedures documented in Deployment Model v0.3. Intel Mac laptop is not a CAS development machine; two-machine workflow not applicable for Phase 1. CAS repository initialized 2026-03-30 at ~/repos/cas/, pushed to private GitHub remote (clifguy/cas). First commit: Formal Substrate v0.1.
- **Claude Code transition.** Completed 2026-03-31. CLAUDE.md files deployed to four locations: user-level (~/.claude/CLAUDE.md), repo root (~/repos/cas/CLAUDE.md), docs/fs/, and domains/pim_health/. CPML v3.0 specification created at ~/Documents/cpml_v3_0.md with symlink into ~/.claude/ for universal access across repos. Architecture reference documents (.docx) to be committed to docs/ref/. CAS ADR store to be committed to docs/. Project tracker to be committed to repo root.
- **Obsidian evaluation.** Possible replacement of OneNote with Obsidian. Plain Markdown file storage makes SAGE integration trivial through the existing adapter.

---

## Sequence

1. ~~Batch document corrections~~ (completed in v0.2 revisions)
2. ~~ADR Store cleanup~~ (completed 2026-03-25, v1.6)
3. ~~CAS Overview v1.0~~ (completed 2026-03-25)
4. ~~CAS Formatting Standards v0.1~~ (completed 2026-03-25)
5. ~~CAS ADR disambiguation~~ (completed 2026-03-26, all four architecture documents)
6. ~~Git setup~~ (completed 2026-03-26, Mac Mini; SGC repo as learning project)
7. ~~Formal Substrate v0.1~~ (completed 2026-03-30, ten configuration schemas)
8. ~~PIM Health Instance v0.1~~ (completed 2026-03-30, five YAML config files)
9. ~~Formal Substrate v1.0~~ (completed 2026-03-31, API contracts and data model schemas)
10. ~~Deployment Model v1.0~~ (completed 2026-03-31, Claude Code transition)
11. ~~Claude Code transition~~ (completed 2026-03-31, CLAUDE.md files and CPML v3.0)
12. Commit documentation artifacts to repo (docs/ref/, docs/cas_adr_store.json, docs/cpml_v3_0.md, tracker) ← **next**
13. Test Plan
14. Working Code
