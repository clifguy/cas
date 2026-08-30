# CAS Application Edge Inference Tests

Tier 2 behavioral tests for the CAS Application's edge inference subsystem.
Each test encodes a design decision made during edge inference specification
(2026-04-08).

Tests are grouped by component in dependency order: filename parser first
(consumed by all other components), then inference methods (version_chain,
filename_code_match), then two-phase orchestration.

---

## 1. Filename Parser: Segment Recognition

### TEST-APP-EI-001: Parse standard filename with all segments

**Artifact:** Project tracker (edge inference design decisions)
**Category:** filename_parser

**Decision:** The filename parser is tolerant and content-aware. It recognizes
date (YYYY-MM-DD pattern), version (v-prefix from right), project (known
identifier), and codes (from known_code_patterns list in vault config). All
segments are nullable except title. Codes are extracted as a list.

**Precondition:** Vault config with known_code_patterns `["^[A-Z]{2,8}$"]` and
filename_extraction pattern `{date}_{project}_{code}_{title}_{version}`.

**Input:** Filename `2026-03-09_EXAMPLE_PV06_Claim-Set_v6_12.docx`

**Expected:**
- `date`: `"2026-03-09"`
- `project`: `"EXAMPLE"`
- `codes`: `["PV06"]`
- `title`: `"Claim-Set"`
- `version`: `"v6_12"` (raw string; normalization is a separate step)
- `doc_type`: resolved via code_to_doc_type rules

**Rationale:** The standard Example Portfolio filename format exercises all five segment
types. The parser must handle the underscore separator while preserving hyphenated
title segments.

### TEST-APP-EI-002: Parse filename missing date -- st_mtime fallback

**Artifact:** Project tracker (edge inference design decisions)
**Category:** filename_parser

**Decision:** Files without a date segment (no YYYY-MM-DD pattern found) use the
source file's st_mtime as the date fallback. The date field is nullable in the
parsed result; the fallback is stored as source_modified_at in the scan result.

**Precondition:** Vault config as above.

**Input:** Filename `EXAMPLE_REF_Glossary_v10.docx` with st_mtime `2026-02-15T10:30:00`

**Expected:**
- `date`: null
- `source_modified_at`: `"2026-02-15T10:30:00"` (from st_mtime)
- `project`: `"EXAMPLE"`
- `codes`: `["REF"]`
- `title`: `"Glossary"`
- `version`: `"v10"`

**Rationale:** Legacy files and externally sourced documents often lack date codes.
st_mtime provides a reasonable ordering proxy.

### TEST-APP-EI-003: Parse filename missing version

**Artifact:** Project tracker (edge inference design decisions)
**Category:** filename_parser

**Decision:** Version is nullable. When no v-prefix segment is found scanning from
the right, the version field is null. Documents with null version participate in
code matching but not version chaining.

**Precondition:** Vault config as above.

**Input:** Filename `2026-03-15_EXAMPLE_TD_Neural-Pathway-Analysis.docx`

**Expected:**
- `date`: `"2026-03-15"`
- `project`: `"EXAMPLE"`
- `codes`: `["TD"]`
- `title`: `"Neural-Pathway-Analysis"`
- `version`: null

**Rationale:** First drafts and single-version documents have no version indicator.
The parser must not force a segment into the version field.

### TEST-APP-EI-004: Parse filename with multiple codes

**Artifact:** Project tracker (edge inference design decisions)
**Category:** filename_parser

**Decision:** Documents may carry multiple codes with different roles. The codes
field is always a list. Multiple segments matching known_code_patterns are all
captured.

**Precondition:** Vault config with known_code_patterns
`["^[A-Z]{2,8}$", "^[A-Z]+-\\d+$"]`.

**Input:** Filename `2026-03-20_EXAMPLE_PV06_CF-1_Integration-Catalog_v3.docx`

**Expected:**
- `codes`: `["PV06", "CF-1"]`
- `title`: `"Integration-Catalog"`
- Both codes available for code_to_doc_type and filename_code_match evaluation

**Rationale:** The Example Portfolio portfolio uses compound code filenames (e.g.,
PV06 + CF-1 for cross-reference integration points). Each code independently
participates in edge inference.

### TEST-APP-EI-005: Parse filename with only title

**Artifact:** Project tracker (edge inference design decisions)
**Category:** filename_parser

**Decision:** When no date, project, version, or code segments are recognized,
the entire filename stem (minus extension) becomes the title. All optional
segments are null and codes is an empty list.

**Precondition:** Vault config as above.

**Input:** Filename `Meeting-Notes-March.md`

**Expected:**
- `date`: null
- `project`: null
- `codes`: `[]`
- `title`: `"Meeting-Notes-March"`
- `version`: null
- `doc_type`: null (no code to match)

**Rationale:** External documents, ad-hoc notes, and non-conforming filenames
must parse without error. The parser is tolerant by design.

### TEST-APP-EI-006: Date recognition requires YYYY-MM-DD pattern

**Artifact:** Project tracker (edge inference design decisions)
**Category:** filename_parser

**Decision:** Only the YYYY-MM-DD pattern (four digits, dash, two digits, dash,
two digits) is recognized as a date segment. Other date-like strings (MM-DD-YYYY,
YYYYMMDD, etc.) are not treated as dates.

**Precondition:** Vault config as above.

**Input:** Filenames:
- `2026-03-15_EXAMPLE_REF_Doc_v1.docx` (valid date)
- `03-15-2026_EXAMPLE_REF_Doc_v1.docx` (MM-DD-YYYY, not recognized)
- `20260315_EXAMPLE_REF_Doc_v1.docx` (compact, not recognized)

**Expected:**
- First: `date` = `"2026-03-15"`
- Second: `date` = null (segment not recognized as date)
- Third: `date` = null (segment not recognized as date)

**Rationale:** Strict date pattern prevents false positives on numeric segments
that happen to be 8+ digits. The Example Portfolio naming convention uses YYYY-MM-DD.

### TEST-APP-EI-007: Version recognition scans from right

**Artifact:** Project tracker (edge inference design decisions)
**Category:** filename_parser

**Decision:** Version is identified by a v-prefix, scanning segments from right
to left. The rightmost v-prefixed segment is the version. This prevents matching
a v-prefixed word in the title (e.g., "Validation" would not match because it
lacks the segment boundary).

**Precondition:** Vault config as above.

**Input:** Filename `2026-03-09_EXAMPLE_PV06_Validation-Report_v3.docx`

**Expected:**
- `version`: `"v3"`
- `title`: `"Validation-Report"` (not consumed as version)
- `codes`: `["PV06"]` (PV06 matches code pattern, not version)

**Rationale:** Right-to-left scan ensures the version is the trailing segment,
consistent with the naming convention where version is always last before the
extension.

### TEST-APP-EI-008: Code recognition uses known_code_patterns from vault config

**Artifact:** Project tracker (edge inference design decisions); FS v1.2
(known_code_patterns)
**Category:** filename_parser

**Decision:** A filename segment is classified as a code if and only if it matches
at least one pattern in the vault's known_code_patterns list. Patterns are matched
case-sensitively (patterns define their own character classes, e.g. `[A-Z]` means
uppercase only). Code recognition is independent of doc_type mapping.

**Precondition:** Vault config with known_code_patterns `["^[A-Z]{2,8}$",
"^[A-Z]+-\\d+$"]`.

**Input:** Filenames:
- `2026-03-09_EXAMPLE_PV06_Title_v1.docx` (PV06 matches `^[A-Z]{2,8}$`)
- `2026-03-09_EXAMPLE_CF-1_Title_v1.docx` (CF-1 matches `^[A-Z]+-\\d+$`)
- `2026-03-09_EXAMPLE_x99_Title_v1.docx` (x99 matches neither pattern)

**Expected:**
- First: `codes` = `["PV06"]`
- Second: `codes` = `["CF-1"]`
- Third: `codes` = `[]`, segment treated as part of title or project

**Rationale:** Vault-configurable code patterns allow each domain to define its
own code vocabulary without modifying parser code.

---

## 2. Filename Parser: Doc Type Resolution

### TEST-APP-EI-009: keyword_to_doc_type evaluated before code_to_doc_type

**Artifact:** Project tracker (edge inference design decisions); FS v1.2
(keyword_to_doc_type)
**Category:** filename_parser

**Decision:** keyword_to_doc_type rules are evaluated first. If a keyword match
fires, code_to_doc_type is skipped. This resolves mis-classification of workflow
artifacts that contain domain-specific codes in their filenames.

**Precondition:** Vault config with:
- keyword_to_doc_type: `[{ keyword: "Checklist", doc_type: "checklist" },
  { keyword: "Work-Plan", doc_type: "work_plan" }]`
- code_to_doc_type: `[{ code: "PV06", doc_type: "design_spec" }]`

**Input:** Filename `2026-03-20_EXAMPLE_PV06_Checklist_v3.docx`

**Expected:**
- `doc_type`: `"checklist"` (from keyword match on "Checklist" in title)
- NOT `"design_spec"` (code_to_doc_type for PV06 is not evaluated)

**Rationale:** A PV06 checklist is a workflow artifact, not a design draft. The
checklist keyword in the title is a stronger signal than the PV06 code, which
indicates the report the checklist governs, not the document's own type.

### TEST-APP-EI-010: code_to_doc_type compound key takes precedence

**Artifact:** `metadata_extraction.schema.json` (code_to_doc_type)
**Category:** filename_parser

**Decision:** Within code_to_doc_type, compound keys (code + title_contains)
take precedence over code-only rules. Rules are evaluated
in order; first match wins. (Unchanged from v0.1 schema, confirmed here.)

**Precondition:** Vault config with Example Portfolio code_to_doc_type rules (REF +
"Glossary" -> glossary, REF alone -> reference_document).

**Input:** Filenames:
- `2026-03-15_EXAMPLE_REF_Glossary_v10.docx`
- `2026-03-15_EXAMPLE_REF_Architecture-QA_v2.docx`

**Expected:**
- First: `doc_type` = `"glossary"` (compound match: REF + "Glossary")
- Second: `doc_type` = `"reference_document"` (code-only match: REF)

**Rationale:** Example Portfolio uses REF as a shared code across multiple document
types. Compound keys disambiguate.

### TEST-APP-EI-011: Case-insensitive keyword matching

**Artifact:** FS v1.2 (keyword_to_doc_type, keyword description)
**Category:** filename_parser

**Decision:** Keyword matching in keyword_to_doc_type is case-insensitive.

**Precondition:** keyword_to_doc_type with `{ keyword: "checklist", doc_type:
"checklist" }`.

**Input:** Filenames:
- `EXAMPLE_PV06_Checklist_v3.docx`
- `EXAMPLE_PV06_CHECKLIST_v3.docx`
- `EXAMPLE_PV06_checklist_v3.docx`

**Expected:** All three resolve to `doc_type` = `"checklist"`.

**Rationale:** Filename casing varies across operating systems and user habits.
Case-insensitive matching prevents false negatives.

### TEST-APP-EI-012: No doc_type when no rules match

**Artifact:** Project tracker (edge inference design decisions)
**Category:** filename_parser

**Decision:** When neither keyword_to_doc_type nor code_to_doc_type produces a
match, doc_type is null. The document is ingested without a type; metadata review
can assign one later.

**Precondition:** Vault config with standard Example Portfolio rules.

**Input:** Filename `2026-03-15_EXAMPLE_UNKNOWN_Report_v1.docx` (UNKNOWN matches a
code pattern but has no code_to_doc_type mapping).

**Expected:**
- `codes`: `["UNKNOWN"]`
- `doc_type`: null

**Rationale:** Unrecognized codes should not prevent ingestion. The metadata
review queue surfaces documents with null doc_type for human classification.

---

## 3. Version Chain Inference (Tier 1, supersedes)

### TEST-APP-EI-013: Version normalization to (major, minor, patch)

**Artifact:** Project tracker (edge inference design decisions)
**Category:** version_chain

**Decision:** Version strings are normalized to a (major, minor, patch) tuple
for comparison. `v7` = (7,0,0), `v10_2` = (10,2,0), `v8_4_1` = (8,4,1).
Underscore or dot separators are both accepted.

**Precondition:** Edge inference engine initialized.

**Input:** Version strings: `"v7"`, `"v10_2"`, `"v8_4_1"`, `"v1.3"`, `"v12"`

**Expected:**
- `"v7"` -> `(7, 0, 0)`
- `"v10_2"` -> `(10, 2, 0)`
- `"v8_4_1"` -> `(8, 4, 1)`
- `"v1.3"` -> `(1, 3, 0)`
- `"v12"` -> `(12, 0, 0)`

**Rationale:** Consistent tuple representation enables unambiguous ordering.
Supporting both underscore and dot separators accommodates different naming
conventions.

### TEST-APP-EI-014: Linear chain -- each version supersedes immediate predecessor

**Artifact:** Project tracker (flagged for discussion: supersedes edge chain
specification)
**Category:** version_chain

**Decision:** Supersedes edges form a linear chain, not an exhaustive graph.
Each version supersedes its immediate actual predecessor, not all prior versions.
Given v1, v3, v7 (gaps allowed), the chain is v7->v3->v1.

**Precondition:** Three files with same title and project, different versions.

**Input:** Files (all new, no existing vault documents):
- `2026-03-09_EXAMPLE_PV06_Claim-Set_v1.docx`
- `2026-03-09_EXAMPLE_PV06_Claim-Set_v3.docx`
- `2026-03-09_EXAMPLE_PV06_Claim-Set_v7.docx`

**Expected:** Edge plan contains exactly 2 supersedes edges:
- `Claim-Set_v7` supersedes `Claim-Set_v3`
- `Claim-Set_v3` supersedes `Claim-Set_v1`
- No edge from v7 to v1

**Rationale:** Linear chains are simpler to reason about and traverse. The
immediate predecessor relationship captures the revision history without
redundant transitive edges.

### TEST-APP-EI-015: Version chain groups by title identity

**Artifact:** Project tracker (edge inference design decisions)
**Category:** version_chain

**Decision:** Version chains are scoped to documents sharing the same title,
project, and doc_type. Documents with different titles, different projects, or
different doc_types are in separate chains even if their version numbers overlap.

**Precondition:** Four files, two titles.

**Input:**
- `2026-03-09_EXAMPLE_PV06_Claim-Set_v1.docx`
- `2026-03-09_EXAMPLE_PV06_Claim-Set_v2.docx`
- `2026-03-09_EXAMPLE_TD_Neural-Analysis_v1.docx`
- `2026-03-09_EXAMPLE_TD_Neural-Analysis_v2.docx`

**Expected:** Two independent chains:
- `Claim-Set_v2` supersedes `Claim-Set_v1`
- `Neural-Analysis_v2` supersedes `Neural-Analysis_v1`
- No cross-title edges

**Rationale:** Version numbers are meaningful only within a document lineage.
v2 of one document does not supersede v1 of a different document.

### TEST-APP-EI-015b: Version chain does not cross doc_type boundary

**Artifact:** Project tracker (edge inference design decisions, 2026-04-25)
**Category:** version_chain

**Decision:** Two documents that share a title and project but have different
doc_types do not form a version chain. The doc_type is part of the chain
identity. Documents with null doc_type group only with other null-doc_type
documents (a conservative default: a chain is only inferred when both ends are
confirmed to be the same kind of document).

**Precondition:** Edge inference engine initialized.

**Input:** Three files with the same title and project, mixed doc_types:
- `Claim-Set_v1.docx` (doc_type: `design_spec`)
- `Claim-Set_v2.docx` (doc_type: `work_plan`)
- `Claim-Set_v3.docx` (doc_type: `design_spec`)

**Expected:** Edge plan contains exactly one supersedes edge:
- `Claim-Set_v3` supersedes `Claim-Set_v1` (both `design_spec`)
- No edges involving the `work_plan` v2

**Rationale:** Tightens the inference rule against false positives where two
unrelated artifacts happen to share a title (e.g., a design draft and a work
plan that governs it, both named "Claim-Set"). A document genuinely changing
type across versions is rare and is better captured manually than risked as an
auto-inferred Tier 1 edge.

### TEST-APP-EI-016: Version chain includes existing vault documents

**Artifact:** Project tracker (edge inference design decisions)
**Category:** version_chain

**Decision:** Existing vault documents are included in the comparison set. If the
vault already contains Claim-Set_v5 and the incoming batch has v6 and v7, the
chain is v7->v6->v5 (with v5 being the existing document's ID).

**Precondition:** Vault contains a document with title "Claim-Set", version v5,
document_id "existing-v5".

**Input:** Batch contains:
- `2026-03-09_EXAMPLE_PV06_Claim-Set_v6.docx`
- `2026-03-09_EXAMPLE_PV06_Claim-Set_v7.docx`

**Expected:** Edge plan:
- `Claim-Set_v7` supersedes `Claim-Set_v6`
- `Claim-Set_v6` supersedes existing document "existing-v5"

**Rationale:** Version chains must span the full document lineage, not just the
current batch. Existing vault documents provide the chain anchor.

### TEST-APP-EI-017: No supersedes edge for single version

**Artifact:** Project tracker (edge inference design decisions)
**Category:** version_chain

**Decision:** A document with no other versions (in the batch or vault) produces
no supersedes edge. Version chaining requires at least two versions of the same
document.

**Precondition:** Vault empty. Batch has one versioned file.

**Input:** `2026-03-09_EXAMPLE_PV06_Claim-Set_v1.docx`

**Expected:** Edge plan contains no supersedes edges.

**Rationale:** A single version has no predecessor to supersede.

### TEST-APP-EI-018: Null version excluded from version chaining

**Artifact:** Project tracker (edge inference design decisions)
**Category:** version_chain

**Decision:** Documents with null version (parser found no v-prefix segment) do
not participate in version chaining. They may share the same title as versioned
documents but are treated as unversioned.

**Precondition:** Three files with same title.

**Input:**
- `2026-03-15_EXAMPLE_TD_Neural-Analysis.docx` (no version)
- `2026-03-15_EXAMPLE_TD_Neural-Analysis_v1.docx`
- `2026-03-15_EXAMPLE_TD_Neural-Analysis_v2.docx`

**Expected:**
- `Neural-Analysis_v2` supersedes `Neural-Analysis_v1`
- Unversioned document produces no supersedes edge
- Chain length is 1 (v2->v1), not 2

**Rationale:** Unversioned documents are ambiguous -- they might be the latest
version or a completely different artifact. Excluding them from chaining avoids
incorrect supersedes relationships.

---

## 4. Filename Code Match Inference (Tier 2, covers)

### TEST-APP-EI-019: Workflow artifact covers content artifact sharing a code

**Artifact:** Project tracker (edge inference design decisions)
**Category:** filename_code_match

**Decision:** filename_code_match fires between workflow artifacts (checklist,
work_plan, session_context, template) and content artifacts (design_spec,
technical_disclosure, glossary, etc.) when they share at least one code.
Edge type is `covers` (Tier 2, staged for review).

**Precondition:** Vault config with Example Portfolio doc_types and edge inference rules.

**Input:**
- `2026-03-20_EXAMPLE_PV06_Checklist_v3.docx` (doc_type: checklist)
- `2026-03-09_EXAMPLE_PV06_Claim-Set_v7.docx` (doc_type: design_spec)

**Expected:** Staging edge plan:
- source: Checklist (workflow artifact)
- target: Claim-Set (content artifact)
- edge_type: `covers`
- Shared code: `PV06`

**Rationale:** A PV06 checklist governs (covers) the PV06 design draft. The
workflow artifact is the source because it describes operations on the content
artifact.

### TEST-APP-EI-020: Direction -- workflow is source, content is target

**Artifact:** Project tracker (edge inference design decisions)
**Category:** filename_code_match

**Decision:** Edge direction is always workflow artifact -> content artifact.
The workflow artifact is the source; the content artifact is the target.

**Precondition:** Same as TEST-APP-EI-019.

**Input:** Same files as TEST-APP-EI-019.

**Expected:**
- source_id resolves to the Checklist document
- target_id resolves to the Claim-Set document
- Not the reverse

**Rationale:** The `covers` edge means "this workflow artifact covers (governs)
this content artifact." Consistent directionality enables traversal queries like
"what workflow artifacts cover this report?"

### TEST-APP-EI-021: Workflow-to-workflow pairs get no automatic edge

**Artifact:** Project tracker (edge inference design decisions)
**Category:** filename_code_match

**Decision:** When two workflow artifacts share a code, filename_code_match does
not fire. Only workflow-to-content pairs produce edges.

**Precondition:** Vault config with Example Portfolio doc_types.

**Input:**
- `2026-03-20_EXAMPLE_PV06_Checklist_v3.docx` (doc_type: checklist)
- `2026-03-20_EXAMPLE_PV06_Work-Plan_v2.docx` (doc_type: work_plan)

**Expected:** No filename_code_match edges between these two documents.

**Rationale:** Workflow artifacts sharing a code are related, but the nature of
their relationship (coordination, dependency) is not deterministic enough for
automatic inference. These edges require manual or orchestrator judgment.

### TEST-APP-EI-022: Content-to-content pairs get no automatic edge

**Artifact:** Project tracker (edge inference design decisions)
**Category:** filename_code_match

**Decision:** When two content artifacts share a code, filename_code_match does
not fire. Content-to-content relationships (e.g., derived_from, references) are
Tier 3 (manual only) or require content_reference analysis (Phase 2).

**Precondition:** Vault config with Example Portfolio doc_types.

**Input:**
- `2026-03-09_EXAMPLE_PV06_Claim-Set_v7.docx` (doc_type: design_spec)
- `2026-03-09_EXAMPLE_PV06_Specification_v3.docx` (doc_type: design_spec)

**Expected:** No filename_code_match edges between these two documents.

**Rationale:** Two report drafts sharing a code might be related versions (handled
by version_chain) or genuinely distinct documents. filename_code_match cannot
distinguish these cases.

### TEST-APP-EI-023: Code match across new files and existing vault documents

**Artifact:** Project tracker (edge inference design decisions)
**Category:** filename_code_match

**Decision:** Existing vault documents are included in the comparison set.
A new workflow artifact can be matched against an existing content artifact
(and vice versa).

**Precondition:** Vault contains a design_spec document with code PV06,
document_id "existing-report".

**Input:** Batch contains:
- `2026-03-20_EXAMPLE_PV06_Checklist_v3.docx` (doc_type: checklist)

**Expected:** Staging edge:
- source: Checklist (new, workflow)
- target: existing-report (existing, content)
- edge_type: `covers`

**Rationale:** Edge inference must span the full vault state, not just the
current batch. A new checklist covering an existing design draft is a valid
and common relationship.

### TEST-APP-EI-024: Multiple codes produce multiple edges

**Artifact:** Project tracker (edge inference design decisions)
**Category:** filename_code_match

**Decision:** A document with multiple codes can match against different documents
on different codes. Each code independently participates in matching.

**Precondition:** Vault config with Example Portfolio rules.

**Input:**
- `2026-03-20_EXAMPLE_PV06_CF-1_Checklist_v1.docx` (doc_type: checklist,
  codes: ["PV06", "CF-1"])
- `2026-03-09_EXAMPLE_PV06_Claim-Set_v7.docx` (doc_type: design_spec)
- `2026-03-09_EXAMPLE_CF-1_Integration-Catalog_v3.docx` (doc_type:
  integration_catalog)

**Expected:** Two staging edges:
- Checklist covers Claim-Set (shared code: PV06)
- Checklist covers Integration-Catalog (shared code: CF-1)

**Rationale:** A cross-reference checklist (PV06 + CF-1) governs both the report
draft and the integration catalog. Each code match produces an independent edge.

---

## 5. Two-Phase Inference Orchestration

### TEST-APP-EI-025: Pre-ingest analysis builds edge plan from file manifest

**Artifact:** Project tracker (edge inference design decisions)
**Category:** orchestration

**Decision:** Phase 1 (pre-ingest) operates on parsed filename metadata from the
scan result plus existing vault documents. It produces an edge plan: a list of
planned edges with source/target identified by file_path (for new files) or
document_id (for existing vault documents), edge_type, inference_method, and
evidence (shared code, version comparison).

**Precondition:** Scan result with parsed metadata for 3 files. Vault has 1
existing document.

**Input:** Scan result containing:
- `EXAMPLE_PV06_Claim-Set_v6.docx` (new, design_spec)
- `EXAMPLE_PV06_Claim-Set_v7.docx` (new, design_spec)
- `EXAMPLE_PV06_Checklist_v3.docx` (new, checklist)
Existing vault: Claim-Set_v5 (document_id: "existing-v5")

**Expected:** Edge plan:
1. supersedes: v7 -> v6 (version_chain, Tier 1)
2. supersedes: v6 -> existing-v5 (version_chain, Tier 1)
3. covers: Checklist -> v7 (filename_code_match, Tier 2)
4. covers: Checklist -> v6 (filename_code_match, Tier 2)
5. covers: Checklist -> existing-v5 (filename_code_match, Tier 2)

**Rationale:** Pre-ingest analysis captures the full set of inferred relationships
before any documents are created. The edge plan is a data structure, not a set of
executed operations.

### TEST-APP-EI-026: Post-ingest creation resolves file paths to document IDs

**Artifact:** Project tracker (edge inference design decisions)
**Category:** orchestration

**Decision:** Phase 2 (post-ingest) resolves file paths in the edge plan to the
document IDs returned by SAGE's ingest endpoint. Tier 1 edges are created via
SAGE link(). Tier 2 edges are inserted into the staging table.

**Precondition:** Edge plan from pre-ingest analysis. All files successfully
ingested with returned document IDs.

**Input:** Edge plan with file_path references. Ingest results mapping file_path
to document_id.

**Expected:**
- Every file_path reference in the edge plan replaced with the corresponding
  document_id
- Tier 1 edges: SAGE link() called for each
- Tier 2 edges: inserted into staging_edges table with inference_evidence
- Edges referencing existing vault documents already have document_ids (no
  resolution needed)

**Rationale:** SAGE document IDs are assigned during ingestion and are not
predictable beforehand. Post-ingest resolution bridges the gap between the
filename-based edge plan and SAGE's ID-based graph.

### TEST-APP-EI-027: Tier 1 edges created via link(), Tier 2 via staging

**Artifact:** Project tracker (edge inference design decisions); edge_inference
vault config (tier_assignments)
**Category:** orchestration

**Decision:** The edge tier determines the creation method. Tier 1 (supersedes)
edges call SAGE's link() directly -- they enter the production graph without
review. Tier 2 (covers) edges are inserted into the staging_edges table for
human review via the Edge Review tab.

**Precondition:** Edge plan with both Tier 1 and Tier 2 edges.

**Input:** Edge plan from TEST-APP-EI-025.

**Expected:**
- Supersedes edges (Tier 1): created in production edge table via link()
- Covers edges (Tier 2): created in staging_edges table
- No Tier 1 edges in staging; no Tier 2 edges in production

**Rationale:** The tier system reflects confidence in automated inference.
Supersedes from version chaining is deterministic (same title, adjacent versions).
Covers from code matching is probabilistic (codes may be coincidental).

### TEST-APP-EI-028: Edge plan handles failed ingestions gracefully

**Artifact:** Project tracker (per-file error isolation)
**Category:** orchestration

**Decision:** If a file fails ingestion, edges involving that file are dropped
from the edge plan during post-ingest resolution. Other edges proceed normally.
The summary event reports both edge creation counts and dropped edge counts.

**Precondition:** Edge plan references 3 files. File 2 fails ingestion.

**Input:** Edge plan from pre-ingest. File 2 ingest returns error.

**Expected:**
- Edges involving file 2 are silently dropped (no error, just omitted)
- Edges between file 1, file 3, and existing vault documents proceed normally
- Summary includes: `edges_dropped` count for edges involving failed files

**Rationale:** Per-file error isolation extends to edge creation. A failed file
should not prevent edge creation between successfully ingested files.

### TEST-APP-EI-029: Empty manifest produces empty edge plan

**Artifact:** Project tracker (edge inference design decisions)
**Category:** orchestration

**Decision:** An empty file manifest (no files selected for ingestion) produces
an empty edge plan with no edges.

**Precondition:** Edge inference engine initialized.

**Input:** Empty file list (after scan filtering).

**Expected:** Edge plan is empty (no planned edges of any type).

**Rationale:** Edge case validation. The inference engine must handle degenerate
inputs without error.

### TEST-APP-EI-030: Single file with no matches produces empty edge plan

**Artifact:** Project tracker (edge inference design decisions)
**Category:** orchestration

**Decision:** A single file with no version peers and no code matches (against
the batch or vault) produces an empty edge plan.

**Precondition:** Empty vault. Batch has one file.

**Input:** `2026-03-15_EXAMPLE_TD_Neural-Analysis_v1.docx` (no existing vault docs,
no other files in batch with code TD or title Neural-Analysis)

**Expected:** Edge plan is empty.

**Rationale:** Not every ingestion produces edges. The absence of edges is a
valid outcome, not an error condition.

---

## 6. Lifecycle Side Effects

### TEST-APP-EI-031: Supersedes edge transitions target to the table's landing state

**Artifact:** Bug fix (2026-04-09); revised 2026-08-29
**Category:** orchestration, lifecycle

**Decision:** When `resolve_and_execute` creates a Tier 1 supersedes edge, it
must also transition the target document's `lifecycle_status` to the state the
vault's lifecycle transition table declares for the `supersede` action
(`"archived"` under the base lifecycle). This ensures that bulk ingest
supersedes edges produce the same lifecycle outcome as the explicit
`update_lifecycles(action="supersede")` path, by construction rather than by
coincidence.

**Precondition:** Edge plan with a Tier 1 supersedes edge. Both source and
target document IDs are resolved. Target is `"active"`.

**Input:** Edge plan:
- `doc-v2` supersedes `doc-v1` (Tier 1, version_chain)

**Expected:**
- Supersedes edge created
- Target document `doc-v1` lifecycle_status updated to `"archived"`
- `updated_at` timestamp refreshed on the target document

**Rationale:** The lifecycle service's `set_lifecycle(action="supersede")` both
transitions the document and creates the edge. The edge inference path creates
the edge but was missing the lifecycle transition, leaving superseded documents
in "active" state. This violates the invariant that a document targeted by a
supersedes edge holds a superseded state.

### TEST-APP-EI-032: Supersedes edge is not created when the target's state forbids it

**Artifact:** Bug fix (2026-08-29)
**Category:** orchestration, lifecycle

**Decision:** If the target's current state neither permits `supersede` nor is
a state a supersession lands in, no edge is created at all: `edges_dropped`
advances and a `supersede_target_not_transitionable` warning names the observed
state and the permitted ones. The check runs *before* the edge write, so the
edge and its lifecycle transition are all-or-nothing.

This supersedes the prior decision, which skipped only the lifecycle update and
created the edge regardless. That left a successor edge pointing at a
predecessor that had never been transitioned — the orphan the atomic insert
closed on the ingest path — with no error and no warning to signal it.

**Precondition:** Edge plan with a Tier 1 supersedes edge. Target document is
`"completed"`, a state the base lifecycle neither supersedes from nor lands a
supersession in.

**Input:** Edge plan:
- `doc-v3` supersedes `doc-v2` (Tier 1, version_chain)
- `doc-v2` has lifecycle_status `"completed"`

**Expected:**
- No supersedes edge created
- `edges_dropped == 1`
- No lifecycle update on `doc-v2`
- One warning, `reason == "supersede_target_not_transitionable"`, whose detail
  names both `"completed"` and `"active"`

**Rationale:** The edge asserts that its target has been superseded. Creating it
against a target that has not been, and cannot be, makes the graph assert
something false. Dropping the edge is the disposition that keeps the invariant;
the warning is what makes the dropped supersession visible in the batch summary.

### TEST-APP-EI-033: Batch ingest with infer_edges=False creates no edges

**Artifact:** `sage/mcp_server.py` (bulk_ingest_document), `app/backend/router.py`
**Category:** orchestration, edge_inference

**Decision:** When `infer_edges` is False, batch ingest skips the
EdgeInferenceEngine entirely. Documents are ingested but no edges are created
and no lifecycle transitions are triggered.

**Precondition:** SAGE vault with at least one existing document (to confirm
existing documents are not affected).

**Input:** Batch ingest of two versioned files (e.g., `EXAMPLE_PV99_Title_v1.md`
and `EXAMPLE_PV99_Title_v2.md`) with `infer_edges: false`.

**Expected:**
- Both documents ingested successfully (HTTP 201 each)
- Zero edges created (edges_created: 0)
- Zero edges staged (edges_staged: 0)
- No lifecycle transitions on any documents
- Summary reports document counts but zero edge activity

**Rationale:** Supports the "ingest first, curate later" workflow where an LLM
agent builds edges post-ingest with semantic understanding, rather than relying
on deterministic filename-based inference.

### TEST-APP-EI-034: Supersede landing state is read from the transition table

**Artifact:** Bug fix (2026-08-29)
**Category:** orchestration, lifecycle

**Decision:** The state a superseded target lands in comes from the table's
`to_state`, not from a literal. A vault declaring `active --supersede--> retired`
lands its superseded documents in `"retired"`.

**Precondition:** Table declares `("active", "supersede", "retired")`. Target is
`"active"`.

**Expected:** Edge created; exactly one lifecycle update, to `"retired"`.

**Rationale:** Under the base lifecycle a hardcoded `"archived"` and the
table-derived value coincide, so only a table with a different `to_state`
distinguishes a derived implementation from a hardcoded one.

### TEST-APP-EI-035: Supersede is permitted from any state the table declares

**Artifact:** Bug fix (2026-08-29)
**Category:** orchestration, lifecycle

**Decision:** The precondition is the set of states the table declares a
`supersede` transition from, not the literal `"active"`. A vault declaring
`completed --supersede--> archived` supersedes a `"completed"` target.

**Precondition:** Table declares supersede from both `"active"` and
`"completed"`. Target is `"completed"`.

**Expected:** Edge created; target transitioned to `"archived"`.

**Rationale:** The permissive direction of EI-032's property. A hardcoded
`== "active"` precondition silently skips a transition the vault declares legal.

### TEST-APP-EI-036: No supersedes edge outlives an unsuperseded target

**Artifact:** Bug fix (2026-08-29)
**Category:** orchestration, lifecycle

**Decision:** Across a whole batch, every Tier 1 supersedes edge written leaves
its target holding a superseded state. This is the invariant the pre-flight
gate exists to hold, and it is asserted over the targets actually linked rather
than over a count fixed in advance.

**Precondition:** A batch mixing a permitted supersede (`"active"` target), an
already-superseded one (`"archived"` target), a forbidden one (`"completed"`
target), and an unrelated Tier 2 `covers` edge.

**Expected:**
- No linked supersedes target is left outside a superseded state
- The permitted and already-superseded targets are both linked; the forbidden
  one is not
- The Tier 2 edge is still staged (the gate is scoped to Tier 1 supersedes)
- `edges_dropped == 1`

**Rationale:** The single-edge tests each pin one case; only a mixed batch shows
that the gate discriminates between them rather than applying one disposition
to all.

### TEST-APP-EI-037: Supersedes edge dropped when the target document is absent

**Artifact:** Bug fix (2026-08-29)
**Category:** orchestration, lifecycle

**Decision:** A target that does not resolve to a document has no state to check
against the table, so the supersession cannot be settled and no edge is created.

**Precondition:** Edge plan with a Tier 1 supersedes edge whose target id is not
present in the store.

**Expected:** No edge; `edges_dropped == 1`; one `supersede_target_missing`
warning whose detail names the document id.

**Rationale:** Previously this fell through the same silent branch as a
non-active target, creating an edge against a document that does not exist. The
reason is its own rather than `supersede_target_not_transitionable`, which is a
statement about a document's lifecycle state; an absent document has no state to
report against the table's permitted set.

### TEST-APP-EI-038: Superseding an already-superseded target needs no write

**Artifact:** Bug fix (2026-08-29)
**Category:** orchestration, lifecycle

**Decision:** A target already holding a state a supersession lands in gets the
edge and no lifecycle write, with no warning. The edge is sound — the
predecessor already holds the state the edge asserts of it — and there is
nothing left to transition.

**Precondition:** Base table. Target is `"archived"`.

**Expected:** Edge created; `edges_dropped == 0`; no lifecycle update; no
warnings.

**Rationale:** Chain repair reaches this routinely: inserting an intermediate
version re-points a supersedes edge at a predecessor archived by the
supersession the repair replaces. Gating it would break chain repair, and
warning on it would make every repair noisy.

### TEST-APP-EI-039: Landing-state recognition is table-derived

**Artifact:** Bug fix (2026-08-29)
**Category:** orchestration, lifecycle

**Decision:** The set of states treated as "already superseded" (EI-038) is
derived from the table's `supersede` landing states, not from the literal
`"archived"`.

**Precondition:** Table declares `("active", "supersede", "retired")`. One
target is `"retired"`, another is `"archived"`.

**Expected:** The `"retired"` target is linked with no write; the `"archived"`
target — inert under this table — is gated and dropped.

**Rationale:** Under this table `"archived"` is an ordinary non-superseding
state, so an implementation that hardcodes the landing set fails in both
directions at once.

### TEST-APP-EI-040: A failed target read refuses the edge under its own reason

**Artifact:** Bug fix (2026-08-29)
**Category:** orchestration, lifecycle

**Decision:** When the pre-write read of a supersedes target raises, the edge is
refused under `supersede_target_read_failed` — distinct from
`lifecycle_transition_failed`, which means the edge landed and only the
subsequent lifecycle write failed.

**Precondition:** Edge plan with a Tier 1 supersedes edge; the store raises on
`get_document`.

**Expected:** No edge; `edges_dropped == 1`; one `supersede_target_read_failed`
warning carrying the underlying error text.

**Rationale:** The two conditions leave the graph in opposite states — no edge
versus an edge with an untransitioned target — so a caller triaging warnings has
to be able to tell them apart. Sharing one reason made that impossible.
