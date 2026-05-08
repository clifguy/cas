# Formatting Standards v2.0 Drift Inventory

**Date:** 2026-05-08
**Author:** Clif (with Cowork assistance)
**Status:** Draft for review before any rewrite plan or .docx edits begin
**Predecessor under audit:** `docs/ref/2026-03-30_CAS_REF_Formatting-Standards_v1_0.docx`
**Target version:** v2.0 (promotion paralleling SAGE Architecture Reference v2.0, Deployment Model v2.0, and Overview v2.0)

## Purpose

This inventory identifies the gap between (a) the Formatting Standards v1.0 as authored on 2026-03-30 and (b) the conventions actually operating across CAS reference documents as of 2026-05-08. The Formatting Standards is the natural codification site for the authoring conventions that have been operating during this round of REF doc revisions: no-ADR-Index appendix, citation discipline, scope discipline, no-historical-framing-in-body, single-appendix naming, and the no-version-specific-citation rule. Until v1.0 is revised, these conventions remain undocumented rules that propagate by example.

The principal drift falls into three categories: (1) §11 ADR Index Appendix and Appendix A (the document's own ADR Index) directly prescribe a convention that is now abolished; (2) §10 Revision History Appendix's appendix-letter rule is stale under the no-ADR-Index regime; (3) the document does not yet codify the authoring conventions established this round.

## Audit basis

| Source | State at audit time |
|---|---|
| Formatting Standards v1.0 (.docx) | Issued 2026-03-30 |
| SAGE Architecture Reference (current) | Issued 2026-05-08; demonstrates the no-ADR-Index, single-appendix, citation-discipline, scope-discipline, and no-historical-framing-in-body conventions |
| Deployment Model (current) | Issued 2026-05-08; same conventions, with §8 demonstrating scope discipline (Claude Code and Claude Cowork removed as out-of-scope developer tools) |
| CAS Application Spec (current) | Issued 2026-05-08; same conventions |
| CAS Overview (current) | Issued 2026-05-08; codifies Single source of truth, Pointer direction, and Architectural-vs-deployment distinction as Design Principles 9, 10, and 11 |
| Project Tracker | v96, last updated 2026-05-08 |
| ADR store | 21 ADRs through ADR-021 (2026-05-01); ADRs 022 and 023 in proposed status |

The four newly-revised REF docs are the live exemplars of the conventions; the Formatting Standards rewrite codifies what they jointly demonstrate.

## Citation discipline

REF .docx files are authoritative for the architecture and authoring record; this inventory follows the same discipline. CLAUDE.md is reserved for steering Claude and is not cited as a source. Specific document versions are not cited inline because they go stale; the inventory refers to "current" versions of sister REF docs by name. Specific ADRs are cited inline by ID where load-bearing.

## Scope discipline

The Formatting Standards describes formatting and authoring conventions for CAS reference documents and (where noted) instantiation deliverables. The user's choice of MCP client, browser, or developer tools is out of scope. The standards apply only to CAS-portfolio documents; domain instantiations with their own formatting standards (the project tracker mentions "PIM patent documents" as an example) operate under their own conventions.

## Classification key

- **D — Drift.** The doc claims X; reality is Y. Resolution: rewrite to reflect reality.
- **R — Missing rationale.** A current convention is reflected in operation but the why-it-is-so was captured only in the Overview's Design Principles or in the v2.0 round's emergent practice. Resolution: incorporate into the prose with a citation to the Overview's principles list when load-bearing.
- **S — Missing structure.** An authoring convention that operates across the four newly-revised REF docs but that v1.0 does not cover. Resolution: write a new section.
- **N — No drift.** Verified consistent with current state.

Remediation classifications: **replace**, **incorporate-with-citation**, **new-section**, **no-change**, **remove**.

## Summary

Overall severity: **medium-high**, concentrated at §10 (revision history appendix-letter rule stale), §11 (ADR Index Appendix prescription abolished), Appendix A (the document's own ADR Index, abolished), and absent coverage of the authoring conventions that operate across the four newly-revised REF docs.

| §  | Section | Currency | Principal drift |
|---|---|---|---|
| Cover | Title page | Stale-low | Date update |
| 1 | Purpose and Scope | Mostly current | Verify wording aligns with new framing |
| 2 | File Naming Conventions | Mostly current | Section content holds; verify |
| 3 | Page Setup | Current | No-change |
| 4 | Typography | Mostly current | §4.4 typographical conventions remain accurate; could clarify the document-name italicization rule's interaction with no-version-specific-citation |
| 5 | Paragraph Styles | Current | No-change |
| 6 | Tables | Current | No-change |
| 7 | Running Headers and Footers | Current | No-change |
| 8 | Cover Page | Current | No-change |
| 9 | Cross-References and Fields | Mostly current | Add no-version-specific-citation rule for inter-document references |
| 10 | Revision History Appendix | Stale-medium | Appendix-letter rule presumes a multi-appendix document structure that no longer exists; reframe for the single-appendix default |
| 11 | ADR Index Appendix | Stale (abolished) | Replace entirely with the no-ADR-Index rule |
| 12 | Programmatic Editing Conventions | Current; light update | Acknowledge xml.etree.ElementTree alongside python-docx; emphasize unique paragraph IDs in newly-inserted content |
| New | Authoring Conventions (proposed new section) | Missing | Codify citation discipline, scope discipline, no-historical-framing-in-body, generic-cross-reference rule |
| App. A | ADR Index | Remove | Per the no-ADR-Index convention this document codifies |
| App. B | Revision History | n/a | Rename heading from "Appendix B" to "Appendix"; append v2.0 entry |

## Per-section inventory

### Cover

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.0.1 | D | Cover date "March 30, 2026" → date of v2.0 finalization (2026-05-08). | Replace. |
| 4.0.2 | n/a | Subtitle "Operational Reference Document" — preserved in current REF docs. | No-change. |

### §1 Purpose and Scope

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.1.1 | N | "This document establishes formatting standards for all documents in the CAS (Clif's Agentic System) project. They do not govern documents within domain instantiations that have their own formatting standards, such as PIM patent documents." Current. The PIM-patent-documents framing remains accurate as an example of an out-of-scope instantiation. | No-change. |
| 4.1.2 | N | "All CAS project documentation is kept in Word (.docx) files. Markdown is used for internal workflow documents." Current. | No-change. |

### §2 File Naming Conventions

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.2.1 | N | Standard format YYYY-MM-DD_CAS_REF_[ShortTitle]_v[M]_[m].docx. Verifiable across the current REF set. | No-change. |
| 4.2.2 | N | Document type codes (REF, INST). Current. The instantiation type-code-applies-to-directory-name accommodation remains in force. | No-change. |
| 4.2.3 | N | "The date reflects the last edit of any kind." Current; consistent with the v2.0 round (the v2.0 docs all carry 2026-05-08 file dates). | No-change. |
| 4.2.4 | N | Two-level versioning (MAJOR/MINOR). v1.0 = complete coverage; pre-1.0 = working draft. The four current REF docs (SAGE Arch v2.0, Deployment Model v2.0, App Spec v1.0, Overview v2.0) follow this. | No-change. |

### §3 Page Setup

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.3.1 | N | Paper size, margins, content width, header/footer margins. Current. | No-change. |

### §4 Typography

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.4.1 | N | §4.1 Theme Fonts (Aptos Display / Aptos via Office theme). Current. | No-change. |
| 4.4.2 | N | §4.2 Heading Hierarchy (Title, Subtitle, Heading 1-4 with sizes, colors, weights, spacing). Current. | No-change. |
| 4.4.3 | N | §4.3 Section Numbering (Word multilevel list bound to Heading styles via pStyle; Heading 1 paragraphs that should be unnumbered set numId=0). Current. The v2.0 round's revision scripts implemented this convention correctly. | No-change. |
| 4.4.4 | N | §4.4 Typographical Conventions: smart quotes, no em dashes, italic for document names, ADR references in parentheses. Current; the no-em-dash rule is consistent with user preference and was applied across the v2.0 round. | No-change. |
| 4.4.5 | R (optional) | The "italic for document names" rule applies to inline document names. With the new no-version-specific-citation rule (4.9.2 below), this convention combines: italicize the generic document name, do not include a version label inline. The two rules might land cleanly together if §4.4 cross-references §9. | Optional incorporate. |

### §5 Paragraph Styles

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.5.1 | N | Style reference table (Title, Subtitle, Heading 1-3, Body Text, List Paragraph, Normal). Current; consistent across the v2.0 REF set. | No-change. |
| 4.5.2 | N | Usage notes (Body Text vs. Normal; bold lead-in pattern; Word numbering for lists). Current. | No-change. |

### §6 Tables

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.6.1 | N | Table specifications (header fill, text, borders, padding, width mode). Current. | No-change. |
| 4.6.2 | N | "Both the table-level columnWidths and each cell's width property must be set" and "Use ShadingType.CLEAR (not SOLID) for fill colors." Current. | No-change. |

### §7 Running Headers and Footers

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.7.1 | N | Running header format "CAS REF-[ShortTitle] v[M].[m]". The v2.0 round followed this. | No-change. |
| 4.7.2 | N | Page footer (page number centered). Current. | No-change. |
| 4.7.3 | N | Cover page suppresses both header and footer via "Different First Page". Current. | No-change. |

### §8 Cover Page

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.8.1 | N | Cover page elements (Title, Subtitle, Document class, Date), centered, with page break following. Current. | No-change. |
| 4.8.2 | N | Document class options ("Operational Reference Document" or "Architecture Reference Document"). The current REF set uses both: SAGE Architecture Reference and CAS Application Spec use "Architecture Reference Document"; Overview, Deployment Model, and Formatting Standards use "Operational Reference Document". The choice is per-document and remains a per-document author decision. | No-change. |

### §9 Cross-References and Fields

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.9.1 | N | Internal cross-references must use Word's REF field mechanism (bookmarks + REF fields), never hardcoded section numbers. Current; consistent with the v2.0 round's approach. | No-change. |
| 4.9.2 | S | Add a no-version-specific-citation rule for inter-document references. When citing another CAS reference document in prose, use the document's generic name (italicized per §4.4) without a version label. Document versions evolve; generic names remain stable. The four current REF docs follow this convention (cross-references read "the SAGE Architecture Reference," not "SAGE Architecture Reference v2.0"). | New content; one paragraph in §9. |
| 4.9.3 | N | "Preserve existing fields. Do not overwrite field codes (w:fldChar / w:instrText) with static text." Current; this rule was load-bearing for the v2.0 round's revision scripts. | No-change. |
| 4.9.4 | N | "Create proper fields for new references." Current. | No-change. |
| 4.9.5 | N | "TOC field. Each document's Table of Contents is a TOC field. After editing, the reader updates the TOC in Word." Current; this is exactly the on-open refresh pattern that has been used in delivery for every v2.0 REF doc this round. | No-change. |

### §10 Revision History Appendix

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.10.1 | D | §10.1 Appendix Title rule: "If preceded by an ADR Index (Appendix A), it is Appendix B. If Revision History is the only appendix, it is Appendix A." Stale under the no-ADR-Index regime. The new convention: with no ADR Index appendix, Revision History is the sole appendix and the heading is "Appendix" (no letter suffix), per the SAGE Arch v2.0, Deployment Model v2.0, App Spec v1.0, and Overview v2.0 examples. The appendix-letter rule should apply only when a document has multiple appendices for some other reason. | Replace. |
| 4.10.2 | N | §10.2 Entry format (`v[M].[m] (YYYY-MM-DD): Description.` with version-and-date in bold; ISO date format; inline parenthetical numbering for multiple changes; ADR references by number; oldest entry first; rationale content). Current; consistent with the v2.0 round's entries. | No-change. |
| 4.10.3 | S | Add a "do not revise existing entries" rule. The v2.0 round has consistently appended new entries without modifying existing ones; this rule should be explicit. | New content; one bullet in §10.2. |

### §11 ADR Index Appendix

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.11.1 | D | The entire §11 prescribes the ADR Index Appendix format. Per the no-ADR-Index convention now operating across SAGE Architecture Reference, Deployment Model, CAS Application Spec, and CAS Overview, reference documents do not include an ADR Index appendix. The cas_adr_store.json is the authoritative, machine-consumable, never-stale index. The whole §11 should be replaced with a one-paragraph statement of the rule. | Replace. |
| 4.11.2 | n/a | Proposed replacement content: "Reference documents do not include an ADR Index appendix. The cas_adr_store.json is the authoritative, machine-consumable, never-stale index of CAS architecture decisions, with full options-considered, rationale, and consequences for each ADR. Reference documents cite specific ADRs inline by ID where the decision is load-bearing for the prose; any document-resident enumeration is guaranteed to drift relative to the store. The §10 Revision History appendix is consequently the sole appendix in any reference document." Heading rename from "ADR Index Appendix" to "ADR Citation" or similar to reflect the inline-citation pattern (or keep the heading as a marker that the rule lands here). | Replace (per the proposed content). |

### §12 Programmatic Editing Conventions

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.12.1 | R | "CAS documents are edited programmatically via the unpack/edit XML/repack workflow using python-docx utilities." Current at the conceptual level, but the v2.0 round used `xml.etree.ElementTree` (Python stdlib) and `lxml` for direct XML manipulation, with python-docx as one option among several rather than the canonical tool. Update to acknowledge the alternatives. | Light update; acknowledge ElementTree/lxml. |
| 4.12.2 | N | "Read paragraph styles and field codes to understand how the document renders to humans before making changes." Current; this rule was load-bearing for the v2.0 round. | No-change. |
| 4.12.3 | N | Smart-quote XML entities, run-property preservation, unique bookmark IDs, validate-after-packing. All current. | No-change. |
| 4.12.4 | S (optional) | The v2.0 round's scripts deal with paraId and textId attributes (Word-internal IDs that should be unique within a document). When inserting new paragraphs programmatically, copying the source paragraph's pPr block is sufficient for style continuity but the IDs will be cloned, which Word tolerates. The convention "uniqueness of paraId is enforced by Word's mechanical conformance check" is captured separately by the pim-mechanical-conformance discipline; mention here only if needed. | Optional incorporate. |

### NEW §X Authoring Conventions (proposed new section)

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.X.1 | S | Codify citation discipline as an explicit section. Items: (a) REF .docx files are authoritative for the architecture and operational record; CLAUDE.md is reserved for steering Claude and is not cited as a source authority; (b) Specific document versions are not cited inline because they go stale (the rule duplicates 4.9.2 and may live there alone); (c) The cas_adr_store.json is the authoritative index of CAS ADRs; reference documents cite ADRs inline by ID where load-bearing; (d) Cross-references between REF docs use the receiving document's generic italicized name. | New section. |
| 4.X.2 | S | Codify scope discipline. CAS reference documents describe CAS — its architecture, deployment, application surface, and portfolio. The user's choice of MCP client, browser, or developer tooling is out of scope except as non-defining examples. Domain-specific content belongs in the relevant instantiation document, not in CAS-portfolio reference documents. | New section. |
| 4.X.3 | S | Codify the no-historical-framing-in-body convention. The body of a reference document describes what is and what is intended without narrative comparison to absent prior states. Phrases like "no longer," "previously," "earlier," "currently," "was deprecated," "relocated from," "now nullable to accommodate" all imply comparison to absent prior states and belong in the Revision History appendix, not in the body. The Revision History appendix is the one place historical framing is appropriate by definition. | New section. |
| 4.X.4 | S | Codify the self-contained narrative principle. A reader of any reference document should understand the relevant CAS architecture from the document alone, without consulting code, commits, or the ADR store. ADRs continue to function as audit trail and conflict-resolution authority but documents must stand alone; substantive rationale is incorporated into the prose with citations rather than left only in ADRs. | New section. |
| 4.X.5 | n/a | The Authoring Conventions section operationalizes the Single source of truth, Pointer direction, and Architectural-vs-deployment distinction Design Principles defined in the CAS Overview. The section should cite those principles by name (without page reference) so a reader can connect the operational rules here with their architectural foundation. | Reference to Overview's Design Principles. |

### Appendix A: ADR Index — REMOVED

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.A.1 | D | Per the no-ADR-Index convention codified by the (rewritten) §11 of this very document, Appendix A is removed. The current Appendix A explicitly says "No ADRs currently affect this document directly. This appendix will be populated when formatting-related architecture decisions are recorded." Under the new rule, the appendix would not be added even if formatting-related ADRs were recorded; ADRs are cited inline by ID in the relevant section instead. | Remove entire Appendix A. |

### Appendix B: Revision History → Appendix

| ID | Type | Item | Remediation |
|---|---|---|---|
| 4.B.1 | D | With Appendix A removed, the sole remaining appendix is renamed "Appendix: Revision History" per the single-appendix-letter convention codified across the v2.0 REF set. The (rewritten) §10.1 Appendix Title rule will reflect this: with no other appendices, the heading is "Appendix" (no letter suffix). | Rename heading. |
| 4.B.2 | n/a | Append a v2.0 entry in the established format. Existing entries v0.1 through v1.0 untouched. | Append. |

## New section recommendations

The drift inventory recommends a single new top-level section ("Authoring Conventions" or similar) that codifies the conventions operating across the v2.0 REF set: citation discipline, scope discipline, no-historical-framing-in-body, and self-contained narrative.

Placement: between §11 (ADR Index Appendix, rewritten as the no-ADR-Index rule) and §12 Programmatic Editing Conventions makes the document flow naturally from formatting/structure (§3-§9) through appendix conventions (§10-§11) and authoring discipline (new §X) to programmatic editing (§12). Alternatively, immediately after §1 Purpose and Scope as a foundational framing section that the rest of the document operationalizes.

My recommendation: place after §11 in a new §12 slot, demoting §12 Programmatic Editing Conventions to §13. This keeps the formatting/structural sections together at the front and the authoring/discipline rules adjacent to the closing programmatic-editing material. The architectural-flow argument is that "what you can see in the document" (formatting, structure, appendices) precedes "how you write the document" (authoring conventions) which precedes "how you edit programmatically" (the python-docx / ElementTree / lxml rules).

## Open questions for the rewrite plan

1. **§11 heading after the rewrite.** Currently "ADR Index Appendix." After the rewrite the section is one paragraph long and articulates the no-ADR-Index rule. Heading options: "ADR Citation" (describes the inline-citation pattern), "ADR Index Convention" (preserves continuity with the old heading), or "ADR References" (matches the §4.4 typographical-conventions terminology). My recommendation: "ADR References" — it matches the §4.4 Typographical Conventions item that already exists ("ADR references. Reference ADRs by number in parentheses: (CAS-ADR-010). No italics.") and consolidates the document's coverage of the ADR-citation pattern.

2. **Authoring Conventions section placement.** Two reasonable placements outlined above. My recommendation is after §11 (so §12 Programmatic Editing Conventions becomes §13). Alternative is right after §1 Purpose and Scope.

3. **Authoring Conventions section depth.** Each of the four conventions (citation discipline, scope discipline, no-historical-framing-in-body, self-contained narrative) could land as a single paragraph or as a Heading 2 subsection with multiple bullets. My recommendation: each as its own Heading 2 subsection with a short rule statement plus a one-bullet list of operational consequences. This makes the section searchable and indexable.

4. **Domain instantiation deliverables under the new conventions.** §1 Purpose and Scope notes that domain-instantiation deliverables with their own formatting standards (e.g., PIM patent documents) operate under their own conventions. The new authoring conventions are CAS-portfolio rules; they do not bind domain-specific deliverables. The rewrite plan should make this scope explicit somewhere — likely in the new Authoring Conventions section's preamble. My recommendation: one sentence in the new section's intro affirming the §1 boundary.

5. **§4.4 Typographical Conventions item on em dashes.** Current rule: "Do not use em dashes." This is consistent with user preference ("Do not use em dashes except in academic writing, unless other punctuation is infeasible"). Should the rule be softened to permit em dashes in academic writing within CAS-portfolio documents? CAS-portfolio documents are not academic writing; the rule as stated is appropriate. My recommendation: no change.

6. **§12 (renumbering to §13) Programmatic Editing Conventions update.** v1.0 names python-docx specifically. The v2.0 round used xml.etree.ElementTree (stdlib) and lxml directly because they offer finer control over fields, namespaces, and structural manipulation than python-docx can express cleanly. Update the introductory statement to acknowledge multiple tooling options without prescribing a single one. My recommendation: light update; "edited programmatically via the unpack/edit XML/repack workflow using python-docx utilities or direct XML manipulation with xml.etree.ElementTree or lxml."

## Closing

This inventory enumerates drift across 12 body sections plus appendices. Severity is medium-high overall, concentrated at §10 (revision-history appendix-letter rule), §11 (ADR Index Appendix prescription, abolished entirely), Appendix A (the document's own ADR Index, abolished), and a new section codifying the authoring conventions. The Formatting Standards' structure is sound; the rewrite is principally a series of in-place updates plus one section rewrite (§11), one new section (Authoring Conventions), one appendix removal (A), and one appendix rename (B → Appendix). Standards from the prior REF doc rounds (self-contained narrative, no historical framing in the body, citation discipline, scope discipline, no ADR-Index appendix) apply uniformly and are paradoxically codified by this revision itself. Target version is v2.0.

End of inventory.
