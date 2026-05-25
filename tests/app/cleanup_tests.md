# CAS Application Backend Cleanup Tests

Tier 2 behavioral tests verifying code hygiene and refactoring safety
for the app/backend modules after multiple rounds of iterative development.

Tests are grouped by concern: import hygiene first, then conversion fidelity.

---

## 1. Import Hygiene

### TEST-APP-CL-001: edge_inference module has no unused imports — RETIRED

**Status:** Retired by T-0138 (2026-05-21).

The `app/backend/edge_inference.py` module was deleted entirely when the
remaining `version_chain` and `filename_code_match` inference rules
relocated to `sage/services/batch_inference.py`. The new SAGE module does
not import `Document`, so the original hygiene concern is structurally
prevented rather than test-enforced.

### TEST-APP-CL-002: scan module uses pathlib exclusively (no os import)

**Artifact:** Code review (2026-04-10)
**Category:** import_hygiene

**Decision:** `scan.py` imports `os` at the top level but uses `pathlib.Path`
for all path operations. The `os` import is vestigial.

**Precondition:** Module importable.

**Input:** Import `app.backend.scan` and inspect module-level names.

**Expected:**
- `os` is not present in the module's namespace
- All path operations continue to use `pathlib.Path`

**Rationale:** Mixed `os`/`pathlib` usage in the same module signals incomplete
migration. Since all operations already use `pathlib`, removing `os` completes
the migration.

### TEST-APP-CL-003: router imports HTTPException at module level

**Artifact:** Code review (2026-04-10)
**Category:** import_hygiene

**Decision:** `router.py` imports `HTTPException` inside two function bodies
(scan_endpoint and ingest_endpoint) rather than at the top of the file alongside
other FastAPI imports. Module-level import is the standard pattern and consistent
with how `APIRouter`, `Request`, and `StreamingResponse` are already imported.

**Precondition:** Module importable.

**Input:** Import `app.backend.router` and check for `HTTPException` in the
module's namespace.

**Expected:**
- `HTTPException` is present in the module's namespace (module-level import)
- Both endpoints still raise HTTPException correctly for invalid inputs

**Rationale:** Inconsistent import style within a single file increases
maintenance friction. The local import was likely a quick fix during
development.

---

## 2. Metadata Conversion Fidelity

### TEST-APP-CL-004: _scan_result_to_response preserves all ParsedMetadata fields

**Artifact:** Code review (2026-04-10)
**Category:** metadata_conversion

**Decision:** The `_scan_result_to_response` helper in `router.py` converts a
`ScanResult` (with its `ParsedMetadata` dataclass) to a `ScanResultResponse`
(with its `ParsedMetadataResponse` Pydantic model). All six metadata fields
must survive the conversion: title, date, project, codes, version, doc_type.

**Precondition:** A ScanResult with all metadata fields populated.

**Input:** ScanResult with:
- title="Claim-Set", date="2026-03-09", project="PIM"
- codes=["PV06", "CF-1"], version="v7", doc_type="patent_draft"

**Expected:**
- Response ParsedMetadataResponse has identical field values
- codes list order preserved
- Null-safe: None values for optional fields pass through as None

**Rationale:** This conversion is exercised on every scan response. Field
omission or type mismatch would silently break the frontend's metadata display.

### TEST-APP-CL-005: _to_file_descriptor preserves all ParsedMetadataResponse fields

**Artifact:** Code review (2026-04-10)
**Category:** metadata_conversion

**Decision:** The `_to_file_descriptor` helper converts an `IngestFileItem`
(with `ParsedMetadataResponse`) to a `FileDescriptor` (with
`ParsedMetadataInput`). All six metadata fields must survive: title, date,
project, codes, version, doc_type.

**Precondition:** An IngestFileItem with all metadata fields populated.

**Input:** IngestFileItem with:
- file_path="/path/to/doc.docx", source_type="docx"
- parsed_metadata: title="Claim-Set", date="2026-03-09", project="PIM",
  codes=["PV06"], version="v7", doc_type="patent_draft"

**Expected:**
- FileDescriptor.parsed_metadata has identical field values
- FileDescriptor.file_path and source_type preserved
- When parsed_metadata is None, FileDescriptor.parsed_metadata is None

**Rationale:** This conversion bridges the HTTP request model to the service
domain model. Field loss here means metadata silently disappears during
ingestion.

### TEST-APP-CL-006: Metadata round-trip ScanResult -> Response -> FileDescriptor preserves all fields

**Artifact:** Code review (2026-04-10)
**Category:** metadata_conversion

**Decision:** The full metadata path through the router -- ScanResult to
ScanResultResponse to IngestFileItem to FileDescriptor -- must preserve all
six metadata fields without loss or transformation. This tests the composition
of CL-004 and CL-005.

**Precondition:** A ScanResult with fully populated metadata.

**Input:** ScanResult with all fields populated, converted through both helpers.

**Expected:**
- Final ParsedMetadataInput field values match original ParsedMetadata values
- No field renaming, reordering, or type coercion occurs
- Round-trip works for both fully populated and sparse (all-None) metadata

**Rationale:** The two conversions are independently maintained but jointly
exercised on every ingest-from-scan flow. A round-trip test catches drift
between the two mapping functions.
