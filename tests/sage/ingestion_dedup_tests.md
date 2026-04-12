# Ingestion Deduplication Tests

Tier 2 behavioral test verifying that re-importing an external file with
identical content does not create a duplicate in the vault's imports/ directory.

---

## TEST-SAGE-BH-071: Same-name same-content re-import reuses existing file

**Artifact:** Bug fix (2026-04-12) -- `_ensure_vault_local` created hash-suffixed
duplicates when re-importing files with identical content.

**Category:** import_hygiene

**Decision:** When an external file is imported and `imports/` already contains a
file with the same name, `_ensure_vault_local` should compare content hashes.
If the content is identical, the existing file is reused (no copy, no hash suffix).
A hash-suffixed copy is created only when the content differs (BH-055).

**Precondition:**
- `imports/report.md` pre-populated with known content
- External file at a different path with the same filename and identical content

**Input:** Ingest the external file via `IngestRequest` with `force=True`
(to bypass `DuplicateContentError` and verify the file-handling path).

**Expected:**
- `source_path` is `imports/report.md` (no hash suffix appended)
- Only one file exists in `imports/` (no duplicate created)
- The file content is unchanged

**Rationale:** Without this check, every re-ingestion of a previously imported
file leaves an orphaned hash-suffixed copy on disk. Over time this doubles the
file count in `imports/`. The PIM vault accumulated 134 such orphans.
