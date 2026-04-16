# SAGE Ingestion Metadata Extraction Tests

Behavioral tests for vault-driven metadata extraction in the SAGE ingestion
pipeline. These tests validate that `IngestionService.ingest` invokes the
filename parser configured in the vault's `metadata_extraction` block and
that parsed fields populate the document record uniformly regardless of
the caller entry point (direct service call, MCP tool, app backend).

Each test encodes a design decision from CAS-ADR-015: "Metadata extraction
is a SAGE-level capability, not a caller responsibility." Before this
decision, `FilenameParser` lived in `app/backend/` and only the CAS
Application's scan/ingest flow invoked it. The `sage_ingest` MCP tool
called `IngestionService.ingest` without parsed metadata, leaving every
filename-derived field blank and letting the adapter's `ProjectionResult.title`
win unconditionally.

Test environment: these tests use the in-memory stub content/embedding
providers from `tests/sage/conftest.py`, real graph store (SQLite on
`tmp_path`), and real source adapters (MarkdownAdapter, DocxAdapter).
Each test gets an isolated temp vault directory.

---

## Precedence model under test

From `docs/fs/sage/metadata_extraction.schema.json`:

> "Precedence: manual entry overrides content extraction overrides
> filename extraction."

In the ingestion flow:

1. **Filename extraction** (lowest precedence): `FilenameParser.parse()` on
   `source_path.stem` using the vault's `metadata_extraction.filename_extraction`
   block. Populates `title`, `document_date`, `project`, `tags` (from codes),
   `version_label`, `doc_type`.
2. **Content extraction** (middle precedence): adapter-provided candidates
   in `ProjectionResult.title` (Word Title style, Markdown H1, etc.). A
   content-extracted title overrides the filename-parsed title ONLY when
   the vault has no filename pattern configured. With a filename pattern
   configured, filename parse wins for title.
3. **Manual entry** (highest precedence): caller-supplied `IngestRequest.metadata`
   overrides everything.

Rationale for filename > adapter-title when pattern is configured: a vault
that declares a filename parse pattern is asserting that filenames carry
authoritative catalog metadata. The adapter's internal title property
(e.g., Word's document.properties.title) reflects the document's rhetorical
title, not its catalog identity. These are different things, and the vault
config is the source of truth for which wins.

---

## TEST-SAGE-ME-001: Filename parse populates document record on direct ingest

**Artifact:** `sage/services/ingestion.py` (IngestionService.ingest)
**Category:** filename_extraction
**Decision:** `IngestionService.ingest` invokes `FilenameParser` on the source
filename using the vault's `metadata_extraction.filename_extraction` config
and populates `title`, `document_date`, `project`, `tags`, `version_label`,
and `doc_type` from the parse result. No caller-supplied metadata is required.

**Precondition:** Vault has PIM-Health-style `metadata_extraction` config
with a filename pattern, `known_code_patterns`, and `code_to_doc_type` rules.

**Input:** `ingest(IngestRequest(source="patents/2026-03-09_PIM_PV06_Claim-Set_v6.md", adapter="markdown"))`

**Expected:**
- `doc.title == "Claim-Set"`
- `doc.document_date == "2026-03-09"`
- `doc.project == "PIM"`
- `doc.tags == ["PV06"]` (codes serialized as tags)
- `doc.version_label == "v6.0"`
- `doc.doc_type == "patent_draft"` (resolved via `code_to_doc_type` PV rule)

**Rationale:** Before CAS-ADR-015, this ingestion path bypassed the filename
parser. All catalog fields except title would be null, and doc_type would
default to "misc". This is the failure mode Cowork observed.

---

## TEST-SAGE-ME-002: Caller-supplied metadata overrides filename parse

**Artifact:** `sage/services/ingestion.py` (IngestionService.ingest)
**Category:** precedence
**Decision:** Values in `IngestRequest.metadata` override values produced
by the filename parser.

**Precondition:** Same as ME-001.

**Input:** `ingest(IngestRequest(source="patents/2026-03-09_PIM_PV06_Claim-Set_v6.md", adapter="markdown", metadata={"title": "Custom Title", "project": "OTHER", "version_label": "v99.0"}))`

**Expected:**
- `doc.title == "Custom Title"` (caller wins)
- `doc.project == "OTHER"` (caller wins)
- `doc.version_label == "v99.0"` (caller wins)
- `doc.document_date == "2026-03-09"` (filename parse fills unspecified field)
- `doc.tags == ["PV06"]` (filename parse fills unspecified field)
- `doc.doc_type == "patent_draft"` (filename parse fills unspecified field)

**Rationale:** Manual entry is the highest-precedence layer. Partial
caller metadata (some fields specified, some not) composes correctly
with filename-parsed values filling the gaps.

---

## TEST-SAGE-ME-003: Filename-parsed title overrides adapter title

**Artifact:** `sage/services/ingestion.py` (IngestionService.ingest)
**Category:** precedence
**Decision:** When the vault has a filename pattern configured and the
parse yields a usable title segment, the filename-parsed title wins over
`ProjectionResult.title` from the adapter.

**Precondition:** Vault has PIM-Health-style filename pattern. Source file's
content produces an adapter title that differs from the filename-parsed
title (e.g., a markdown file whose first H1 differs from the filename stem).

**Input:**
- Source file `2026-03-09_PIM_PV06_Claim-Set_v6.md` with first H1
  "A Long Rhetorical Title That Differs From The Filename".
- `ingest(IngestRequest(source="patents/2026-03-09_PIM_PV06_Claim-Set_v6.md", adapter="markdown"))`

**Expected:**
- `doc.title == "Claim-Set"` (filename parse wins)

**Rationale:** This is the Cowork case. The vault has declared that filenames
carry authoritative catalog metadata, so the filename's title segment is
the authoritative title. The adapter's content-derived title reflects the
document's rhetorical identity, not its catalog identity.

---

## TEST-SAGE-ME-004: No filename pattern -> adapter title is used

**Artifact:** `sage/services/ingestion.py` (IngestionService.ingest)
**Category:** fallback
**Decision:** When the vault's `metadata_extraction` has no
`filename_extraction.pattern` configured, the ingestion service does not
run filename parsing and preserves the adapter's `ProjectionResult.title`.

**Precondition:** Vault uses minimal metadata_extraction config
(`{"review_required": false}` with no filename_extraction block).

**Input:**
- Source file `workflow_notes.md` with first H1 "Session Handoff Notes".
- `ingest(IngestRequest(source="notes/workflow_notes.md", adapter="markdown"))`

**Expected:**
- `doc.title == "Session Handoff Notes"` (adapter H1 wins)
- `doc.doc_type is None` or defaults to "misc" (no filename parse, no caller metadata)
- `doc.document_date` derives from `source_modified_at` (existing BH-063 behavior)
- `doc.project is None`, `doc.tags == []`, `doc.version_label is None`

**Rationale:** Vaults with less-disciplined naming conventions (workflow
artifact vaults) should not have filename parsing forced on them. Absence
of the filename_extraction block is the signal to skip the stage.

---

## TEST-SAGE-ME-005: doc_type does not default to "misc" when filename parse resolves one

**Artifact:** `sage/services/ingestion.py` (IngestionService.ingest)
**Category:** regression
**Decision:** The existing default-to-"misc" path runs only when neither
filename parse nor caller metadata yields a doc_type.

**Precondition:** PIM-Health-style filename pattern. Source filename encodes
a code that maps to a non-misc doc_type via `code_to_doc_type`.

**Input:** `ingest(IngestRequest(source="refs/2026-02-01_PIM_REF_Glossary_v2.md", adapter="markdown"))`

**Expected:**
- `doc.doc_type == "glossary"` (resolved by compound rule: code=REF + title_contains=Glossary)

**Rationale:** Before ADR-015, the misc default masked the presence of
filename-resolvable doc_type values because the filename parser never ran
on this entry point.

---

## TEST-SAGE-ME-006: doc_type defaults to "misc" when no source yields one

**Artifact:** `sage/services/ingestion.py` (IngestionService.ingest)
**Category:** regression
**Decision:** When filename parse runs but yields no doc_type (no matching
code rule, no matching keyword rule) and caller supplies none, the existing
default to "misc" still applies so that content-store pre-filtering has a
stable non-null key.

**Precondition:** PIM-Health-style pattern; source filename contains no code
that matches any `code_to_doc_type` rule.

**Input:** `ingest(IngestRequest(source="random/2026-03-01_PIM_Untagged-Note.md", adapter="markdown"))`

**Expected:**
- `doc.doc_type == "misc"`
- `doc.document_date == "2026-03-01"` (filename parse still ran)
- `doc.project == "PIM"`

**Rationale:** The default-to-"misc" invariant is preserved. The change is
narrow: only when filename parse WOULD resolve a doc_type, the misc default
no longer clobbers it.

---

## TEST-SAGE-ME-007: Markdown adapter benefits from the same filename parse path

**Artifact:** `sage/services/ingestion.py` (IngestionService.ingest)
**Category:** uniformity
**Decision:** Filename extraction is adapter-agnostic. Both markdown and
docx ingestion paths invoke the same FilenameParser and apply the same
precedence.

**Precondition:** Same as ME-001.

**Input:** Two ingest calls against files with structurally identical names
but different extensions:
- `2026-03-09_PIM_PV06_A_v1.md`
- `2026-03-09_PIM_PV06_A_v1.docx` (docx adapter with minimal docx content)

**Expected:**
- Both resulting Documents have matching `title`, `project`, `tags`,
  `version_label`, `document_date`, `doc_type`.
- Only `source_type`, `adapter_version`, and `source_content_hash` differ.

**Rationale:** Confirms the refactor unified the metadata extraction path
rather than only fixing the docx case. Uniformity is the design goal.

---

## TEST-SAGE-ME-008: review_required flag controls metadata_confirmed at ingest

**Artifact:** `sage/services/ingestion.py` (IngestionService.ingest)
**Category:** lifecycle
**Decision:** When the vault's `metadata_extraction.review_required` is
true, the document is created with `metadata_confirmed=False`, inviting
interactive confirmation via `update_metadata`. When false, the document
is created with `metadata_confirmed=True` because the vault has declared
its filename convention trustworthy.

**Precondition:** Two vaults: one with `review_required=True`, one with
`review_required=False`. Same source file pattern.

**Input:** Ingest the same file shape into each vault.

**Expected:**
- Vault with `review_required=False`: `doc.metadata_confirmed == True`.
- Vault with `review_required=True`: `doc.metadata_confirmed == False`.

**Rationale:** The schema's `review_required` flag was defined but unwired
on the direct-SAGE path. This test confirms the flag is now honored at
ingest, not just at update_metadata time.

---

## TEST-SAGE-ME-009: App-backend ingest path continues to produce correct metadata

**Artifact:** `app/backend/ingest_service.py`, `app/backend/scan.py`,
`app/backend/edge_inference.py`
**Category:** regression
**Decision:** The import relocation (FilenameParser moves from `app.backend`
to `sage.services`) does not break the app backend's scan/ingest/edge
inference flow.

**Precondition:** Existing app backend fixtures (`_pim_metadata_extraction()`,
app-backend vault config) unchanged.

**Input:** Re-run the existing `tests/app/test_app_backend.py::TestFilenameParserSegments`
and `TestFilenameParserDocType` tests after the import update.

**Expected:** All existing app backend tests pass without modification to
their assertions.

**Rationale:** The move is a relocation, not a rewrite. Import paths
change; behavior does not.
