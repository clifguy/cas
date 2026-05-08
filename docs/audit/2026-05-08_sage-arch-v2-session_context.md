# Session Continuity Artifact: SAGE Architecture Reference v2.0 Rewrite

**Date:** 2026-05-08 (session active 2026-05-06 through 2026-05-08)
**Topic:** Bringing CAS reference documentation in sync with the as-built state of SAGE and the CAS Application
**Outcome:** SAGE Architecture Reference v2.0 delivered; two architectural ADRs drafted; deployment content separated for the Deployment Model rewrite

---

## 1. Substantive summary

### The problem

The CAS portfolio's reference documents in `docs/ref/` had drifted significantly from the as-built state of SAGE and the CAS Application, both of which had been built with Claude Code over the prior month. ROOT Harness remained unbuilt. The Project Tracker, the ADR store, and the Formal Substrate had been kept current; the `.docx` reference documents had not.

### Standards established this session

Three standards shaped the work:

**Self-contained narrative.** A human reader should be able to read the docs alone and accurately understand what CAS is and how it works for the parts that are built, plus what CAS is intended to be for the parts not yet built. Reading the code, the commits, or the ADRs to understand the architecture is not required. The ADRs continue to function as audit trail and conflict-resolution authority; the documents incorporate substantive rationale into the prose with citations rather than acting as mere pointer hubs.

**No historical framing.** The body of the document describes what is and what is intended without narrative. Phrases like "no longer," "previously," "earlier," "currently," "was deprecated," "relocated from," "now nullable to accommodate" all imply comparison to absent prior states and were systematically removed across §1-§10. The Revision History appendix is the one place where historical framing is appropriate by definition; the ADR Index entries describe what an ADR establishes (current state) rather than what it changed (transition state).

**Architectural-vs-deployment distinction.** An architectural decision establishes a constraint or structure that governs the system across deployments; the decision is portable as the deployment environment changes. A deployment-environment constraint is driven by the specific resources of a particular deployment and would change with different resources. The distinction was applied to determine: (a) which decisions warrant ADRs (architectural) versus deployment-document content (deployment-environment); (b) what content belongs in the Architecture Reference versus the Deployment Model.

### Major decisions made

**CAS-ADR-022 (proposed): Documentation maintained as a SAGE vault with supersedes-edge version history.** CAS reference documents become content within a SAGE vault dedicated to the CAS portfolio, with version history captured via `supersedes` edges in the vault's graph store rather than via filename versioning plus git history. The CAS ADR Store and the Formal Substrate remain in the git repository (they are operational artifacts consumed by code, not narrative content). A new `cas` vault to be provisioned at `~/sage_vaults/cas/`.

**CAS-ADR-023 (proposed): Linear supersedes chain.** The supersedes chain is linear by construction: each version supersedes only its immediate actual predecessor, not all prior versions. Edge count scales linearly rather than quadratically; the resolver's chain-walk is simpler; the mental model matches reviewer intuition. Per-vault configuration explicitly rejected to keep graph semantics uniform across vaults.

**Three deployment-driven items extracted to deployment-notes source material.** Drafted as candidate ADRs early in the session, then reclassified as deployment constraints rather than architectural decisions: sequential ingestion pipeline, lazy abstraction model loading, single-process Phase 1 topology. Their content is preserved at `docs/audit/2026-05-06_deployment-notes-source-material.md` for the Deployment Model rewrite.

### What was delivered

**SAGE Architecture Reference v2.0 (`.docx`).** Comprehensive refresh to as-built state. Substantive incorporation of CAS-ADRs 011 through 021 and Formal Substrate revisions through v1.14. Edge model updated for chain-scoped resolution (CAS-ADR-017): two anchor fields, `valid_until_version`, `retracted_edge_id`, the resolution-policy enumeration, the per-vault edge-type registry, and a worked example in Appendix B. New edge types `instantiated_from`, `retracts`, `merged_from` added to the eleven-edge taxonomy. Source change detection rewritten to remove file watcher (CAS-ADR-018); ingestion is exclusively intentional. Metadata extraction rewritten for caller-owned model with chain inheritance for the type-shaped trio (CAS-ADR-021). Edge inference rewritten with mechanical-vs-curated provenance gate (CAS-ADR-019); linear supersedes-chain semantics stated explicitly. UI-layer file metadata normalization documented (CAS-ADR-016). Retrieval architecture expanded with `catalog` and `keyword` modes, salience reranking, abstract-boosted retrieval, document-level response mode, pre-filter resolution, and the chain walk operation. Lifecycle state machine simplified (`superseded` removed from the base set). Object model gains decision logs (CAS-ADR-012) and the pipeline-status surface. Core API restructured by tag (thirty-two operations across eight subsections, plus the CAS Application API surface). Multi-vault physical layout corrected. Open Design Questions pruned and updated. Cover page preserved from v1.4.2 with the date updated. Headings auto-number through the document's multilevel-list reference; appendices carry the `numId=0` override to suppress numbering. Mechanical conformance: PASS (4 / 0 / 0).

The v1.4.2 file remains in `docs/ref/` as the predecessor in the eventual `supersedes` chain when the `cas` vault is provisioned.

### Manual edits Clif applied to v2.0

After my initial delivery, Clif:

- Removed the ADR Index appendix (originally Appendix C). Rationale: the ADR JSON store is the authoritative index; a duplicate index in the document would be redundant.
- Reordered remaining appendices: Appendix B is now Worked Example (Chain-Scoped Edge Resolution); Appendix C is now Revision History.
- Applied formatting changes throughout.

I then made two corrective passes:

- Restored the Revision History appendix to v1.4.2's prose format (each entry as a `BodyText`-styled paragraph with bold version-and-date prefix, replacing the table form I had introduced).
- Removed deployment content (§3.2.1-3, §3.3.4, §3.3.5, §3.4.3, §3.5.2, §9, plus the trailing deployment-laden paragraph of §3.5.1) and trimmed two tables (§3.3.1, §3.5.1) accordingly. Renamed §3.5.1 from "Protocol Adapters and Transports" to "Protocol Adapters."

After my final pass, Clif addressed three closing observations: TOC field refresh (Word will regenerate), §3.5 single-Heading3-child structural redundancy, and §3.2's brevity now that its children are gone.

---

## 2. Tracked and flagged items

The session opened with the inventory + plan + body rewrite + appendices + ADR drafting + .docx conversion + conformance check sequence. Items flagged or surfaced during the work:

**`;;flag` from session opening:** Pointer-direction-principle clarification (documents are self-contained narrative; ADRs are audit trail and conflict-resolution authority, not the locus where substance lives). Tracked as part of CAS-ADR-022's framing.

**Architecture correction flagged in earlier tracker work:** Linear supersedes chain — captured this session as CAS-ADR-023. Resolved.

**Open Design Questions in the SAGE Arch v2.0 document (carried forward as live items):**

1. Provenance edge versus provenance fields (Phase 3 consideration).
2. Read-access restrictions (Phase 3 consideration).
3. Periodic LanceDB compaction (operational follow-up).
4. Resolution policies for `sync_target` and `authoritative_for` (TBD in the registry).
5. Tags chain inheritance (deferred until a concrete need surfaces).
6. Steward and orchestrator decision-log activation (awaits domain choices and ROOT Harness).
7. Editor-endpoint implementation (deferred until a calling workflow needs it).

---

## 3. Next steps and open questions

### Immediate (before next session begins or as part of session handoff)

- **Commit the audit-folder artifacts and the v2.0 `.docx` to git.** This makes the next session see them as committed working state rather than uncommitted drafts.
- **Decide on the two candidate ADRs.** Either ratify CAS-ADR-022 and CAS-ADR-023 by adding them to `docs/cas_adr_store.json` with status `accepted`, or leave them as `proposed` for further review.
- **Optional cleanup:** delete the `.preflight.json` session artifact alongside the v2.0 `.docx` (sandbox could not delete it; manual cleanup needed).

### Next document in the rewrite sequence

The original sequence proposed: SAGE Arch → App Spec → Overview → Deployment Model → ROOT Harness Arch → Diagram → Formatting Standards.

The Deployment Model has accumulated source material from this session (the deployment content extracted from SAGE Arch v2.0 plus the candidate-ADR drafts that became deployment notes). It may now be the natural next target rather than App Spec, since the Deployment Model rewrite has a head start.

Either way, the rewrite-plan pattern from this session (drift inventory → rewrite plan → prose draft → multi-pass review → `.docx` conversion → mechanical conformance) is replicable for subsequent documents.

### CAS vault provisioning (per CAS-ADR-022)

Once CAS-ADR-022 is ratified and at least the SAGE Architecture Reference v2.0 is stable:

1. Provision the `cas` vault at `~/sage_vaults/cas/` with appropriate `vault_config.yaml`.
2. Ingest existing CAS reference documents.
3. Create `supersedes` chains for documents with multiple existing versions (notably the SAGE Arch v1.0 → ... → v1.4 → v2.0 chain).
4. Future revisions go through the `cas` vault rather than via filename-versioning in `docs/ref/`.

### Open architectural questions surfaced this session (forward-looking)

- The Phase 2 separation of CAS Application backend from SAGE Core API is preserved by code structure; a deployment topology change is the only work needed when Phase 2 begins.
- The architectural surface separation between SAGE Core API and CAS Application API is implicit in the Formal Substrate's two spec files. Whether to make this explicit as an ADR remains an open question (currently captured implicitly via CAS-ADR-008's framing of the Formal Substrate as the canonical contract).

---

## 4. Files created or modified

### Audit-phase artifacts (working files, not for canonical retention)

- `/Users/clifguy/repos/cas/docs/audit/2026-05-06_doc-drift-inventory.md` — the per-document drift inventory produced during the audit phase.
- `/Users/clifguy/repos/cas/docs/audit/2026-05-06_sage-arch-v2-rewrite-plan.md` — the section-by-section rewrite plan for SAGE Arch v2.0.
- `/Users/clifguy/repos/cas/docs/audit/2026-05-06_sage-arch-v2.0-prose.md` — the markdown source of the v2.0 body content, used for pandoc conversion.
- `/Users/clifguy/repos/cas/docs/audit/2026-05-06_candidate-adrs-022-023.md` — the two candidate-ADR drafts awaiting ratification.
- `/Users/clifguy/repos/cas/docs/audit/2026-05-06_deployment-notes-source-material.md` — content extracted from SAGE Arch v2.0 that belongs in the Deployment Model rewrite. Includes content from the three reclassified candidate ADRs (sequential pipeline, lazy MLX, single-process topology) plus the body content removed during the deployment-trim pass.
- `/Users/clifguy/repos/cas/docs/audit/2026-05-08_sage-arch-v2-session_context.md` — this artifact.

### Canonical deliverable

- `/Users/clifguy/repos/cas/docs/ref/2026-05-06_CAS_REF_SAGE-Architecture_v2_0.docx` — SAGE Architecture Reference v2.0. Mechanical conformance: PASS. Awaiting commit and (eventually) ingest into the `cas` vault as the v2.0 supersession of v1.4.

### Predecessor (preserved for the future supersedes chain)

- `/Users/clifguy/repos/cas/docs/ref/2026-03-30_CAS_REF_SAGE-Architecture_v1_4_2.docx` — previous version of record; will become the predecessor link in the `cas` vault's `supersedes` chain when that vault is provisioned per CAS-ADR-022.

### Session-only artifacts (safe to delete)

- `/Users/clifguy/repos/cas/docs/ref/2026-05-06_CAS_REF_SAGE-Architecture_v2_0.docx.preflight.json` — mechanical-conformance report alongside the deliverable. Output of the preflight script; sandbox could not delete it. Safe for manual cleanup.

---

## 5. How to reboot from this artifact

The next session should be able to bootstrap from this file plus the audit-folder artifacts. Suggested priming:

```
;;prime 2026-05-08_sage-arch-v2-session_context
```

The next document to rewrite is either Deployment Model (head start from this session's deployment-notes source material) or CAS Application Spec (next in the original rewrite sequence). The rewrite-plan pattern is replicable. The standards established this session — self-contained narrative, no historical framing, architectural-vs-deployment distinction — apply uniformly to the next document.

End of artifact.
