# CAS-ADR-021 Cleanup Tracker

**ADR:** [CAS-ADR-021](docs/cas_adr_store.json) — Metadata inference and review are caller responsibilities; SAGE accepts authoritative values
**Implementation tracker:** [CAS-ADR-021_implementation.md](CAS-ADR-021_implementation.md) (transient; deleted on implementation completion)
**Status:** awaiting ADR-021 implementation completion before any phase begins
**This file:** transient. Delete when all phases complete and the final commit lands.

---

## Purpose

Cross-session continuity artifact for the cleanup work that follows ADR-021's implementation. The cleanup is structurally separate from the implementation: the implementation tracker covers SAGE behavior changes and the new caller-facing API surface; this tracker covers the coordinated CAS app update that must accompany the SAGE flip, and the subsequent removal of code paths and configuration fields that become vestigial after the flip.

The main project tracker (`CAS_Project_Tracker.md`) carries only a one-line status pointer to this file; detail lives here.

---

## Scope and rationale

ADR-021's implementation makes `needs_review=false` the default on every ingest. Three things become misaligned with the new architecture and need cleanup:

1. **The CAS app's existing call site** (`app/backend/ingest_service.py:152`) constructs `IngestRequest` without passing `needs_review`. With the new default, its bulk ingests would silently bypass the review queue — the opposite of the user-visible behavior the CAS app's UI presents. The CAS app must explicitly pass `needs_review=true` on its bulk-ingest calls.

2. **`metadata_extraction.review_required`** in vault configs becomes a read-but-unused flag. The schema field, the live vault YAMLs, the `IngestionService._review_required` member, the `vault_management.py` default-write, and any frontend code that reads or writes the field all need to be removed.

3. **CAS documentation** (SAGE Architecture Reference, CAS Application Spec, CAS Overview) describes the previous review-queue model. Refresh is part of the broader post-beta documentation consolidation, not scoped here.

These are sequenced as three phases. Phase A is coordination-critical (must accompany or precede the SAGE flip); Phase B is independent cleanup that can land any time after Phase A is stable; Phase C is parked for the post-beta consolidation.

---

## Phase A — CAS app caller-side coordination (REQUIRED with or before the SAGE flip)

**Status:** `complete`
**Coordination constraint:** This phase must land **before or simultaneously with** ADR-021 implementation Chunk 2 (the chunk that flips the default in `services/ingestion.py`). If Chunk 2 ships first, the CAS app's bulk ingests will not enter the `pending_metadata` review queue — the review UI in the CAS app will appear to function but will be empty for all newly-bulk-ingested documents.

**Goal:** Update the CAS app's bulk-ingest call site to explicitly pass `needs_review=true`. This preserves today's user-visible CAS bulk-ingest review behavior under the new SAGE default.

**Critical files:**
- MODIFY `app/backend/ingest_service.py` — at line ~152, add `needs_review=True` to the `IngestRequest(...)` construction in `ingest_files`.

**Tests:**
- The existing CAS app test suite (`tests/app/`) should be reviewed for any test that constructs `IngestRequest` without `needs_review` and asserts on `metadata_confirmed` or `pending_metadata` queue presence. After Phase A, those assertions need to align with the new explicit-flag behavior.

**Verification:**
- After ADR-021 implementation Chunk 2 has landed (or is landed in coordination with this phase): bulk-ingest a document through the CAS application's UI; verify the document appears in the review queue with `metadata_confirmed=false`.
- Anti-coincidental-pass check: temporarily revert the `needs_review=True` addition; confirm the bulk-ingested document does NOT appear in the review queue (the regression case).

**Future enhancement (NOT in Phase A scope):** the CAS app could call the new `sage_parse_filename` endpoint to populate filename-based suggestions before submitting an ingest. This is the architecturally cleaner end-state per ADR-021 and per the ADR-021 implementation Chunk 3 design rationale, but is not required for correctness — passing `needs_review=true` is sufficient to preserve current behavior. Defer to a future CAS app frontend pass.

**Dependencies:** ADR-021 implementation Chunks 1-2 (schema landed and behavior change ready to ship). Phase A may be drafted earlier but cannot be merged until the SAGE flip is ready, otherwise it would be a no-op landing in advance.

**Blockers:** none anticipated.

---

## Phase B — Vestigial code and configuration removal

**Status:** `complete`
**Goal:** Remove the `metadata_extraction.review_required` flag and all dependent code now that the runtime behavior is driven by `request.needs_review` rather than vault config.

**Critical files:**

SAGE-side:
- MODIFY `sage/services/ingestion.py` — remove the `self._review_required` member (`__init__` lines ~262-267) and any remaining references; update or remove the now-stale ME-008 comment block (lines ~559-562).
- MODIFY `sage/vault_management.py` — remove the `"review_required": False` default-write at line ~146 from the vault-creation flow.
- MODIFY `docs/fs/sage/metadata_extraction.schema.json` — remove the `review_required` property from the schema. Bump the schema version field per the manifest convention.
- MODIFY `docs/fs/manifest.json` — add a revision_history entry; bump `substrate_version`.

CAS app frontend:
- MODIFY `app/src/components/Sidebar.tsx` — remove the `review_required: false` payload at line ~91 (vault creation path). Confirm no other frontend code reads or writes the field; remove if found.

Live vault state (out-of-repo):
- UPDATE `~/sage_vaults/pim_health/vault_config.yaml` — remove `metadata_extraction.review_required` if present.
- UPDATE `~/sage_vaults/test_vault/vault_config.yaml` — same.
- UPDATE `~/sage_vaults/theology/vault_config.yaml` — same.
- UPDATE `~/sage_vaults/new_vault/vault_config.yaml` — same.

Tests:
- MODIFY `tests/sage/test_ingestion_metadata_extraction.py` — remove or rewrite TEST-SAGE-ME-008 (lines ~468-540) which exercises the `review_required → metadata_confirmed` mapping that no longer exists. The TEST-AD021 cases from the implementation supersede the ME-008 coverage; ME-008 is dead.
- MODIFY `tests/sage/test_ingestion_metadata_extraction.py` and any other tests that pass `review_required=True/False` into vault config fixtures — strip the parameter; the field no longer exists.
- AUDIT `tests/app/` — any test fixtures that include `metadata_extraction.review_required` in vault configs.

**Dependencies:**
- Phase A must be landed and stable (the CAS app must already be passing `needs_review=true` explicitly so removing the auto-set behavior does not regress its review-queue UX).
- ADR-021 implementation must be complete (all four chunks landed; ADR-021 promoted from `proposed` to `accepted`).

**Verification:**
- Full test suite green: `.venv/bin/python -m pytest tests/sage tests/app`.
- JSON Schema meta-validation clean on the modified `metadata_extraction.schema.json`.
- Each live vault config validates against the updated schema (`python3 -c "import yaml, jsonschema, json; jsonschema.validate(yaml.safe_load(open('CONFIG.yaml')), json.load(open('SCHEMA.json')))"`).
- CAS app bulk-ingest UI continues to function: documents enter the review queue when `needs_review=true` is passed.
- Anti-coincidental-pass check: temporarily revert one of the field-removal edits; confirm that test or schema validation surfaces the inconsistency.

**Blockers:**
- None anticipated. The flag is independent of the broader two-tier metadata model; removing it is mechanical.

---

## Phase C — CAS documentation refresh (post-beta consolidation)

**Status:** `parking lot`
**Goal:** Refresh CAS documentation that references the removed review-queue auto-population model.

**Constraint:** All CAS documentation is currently stale. The user has stated that documentation refresh is deferred to the post-beta consolidation effort, which is broader in scope than ADR-021. This phase exists in the cleanup tracker as a record of *what ADR-021-driven content needs revision* during the consolidation, not as a standalone work item.

**Documents likely affected:**
- `docs/ref/SAGE Architecture Reference.docx` — sections covering ingest pipeline, metadata extraction, and the metadata-confirmation lifecycle.
- `docs/ref/CAS Application Spec.docx` — bulk ingest workflow description.
- `docs/ref/CAS Overview.docx` — any overview-level mention of the review queue or metadata inference.

**ADR-021-specific revision points the consolidation pass should address:**
1. Filename inference is no longer SAGE's responsibility at ingest; it is exposed as a separate library / endpoint / MCP tool (`parse_filename`) that callers invoke before ingest if they want suggestions.
2. The `pending_metadata` review queue is populated only when callers pass `needs_review=true` explicitly; default ingest commits authoritative values.
3. Type-shaped fields (`doc_type`, `project`, `authority_scope`) inherit from the predecessor when the caller omits them and `supersedes_document_id` is set.
4. The `metadata_extraction.review_required` vault config field is removed (after Phase B); any documentation that references this field is stale.

**Dependencies:** Phase B complete; CAS post-beta consolidation effort initiated.

**Blockers:** post-beta consolidation timing is not yet scheduled. This phase is genuinely parked, not pending.

---

## Cross-Cutting Concerns

- **Phase A is coordination-critical.** The single-line change to `app/backend/ingest_service.py` must land in the same merge window as the SAGE default flip. Plan the merges as a coordinated pair.
- **Live vault YAMLs are out-of-repo.** Phase B's vault YAML edits at `~/sage_vaults/*/vault_config.yaml` are operational, not version-controlled. Make a note in the Phase B session log of which vault configs were edited so future audits can verify alignment.
- **Schema-first per principle 8.** Phase B edits the metadata extraction schema first, then runs the validation pass against live vault configs, then deletes references in code.
- **No backwards-compat shims.** Phase B removes the field entirely. Vaults that still carry `metadata_extraction.review_required` after the schema removal will fail validation; the cleanup includes editing the live vault YAMLs to remove the now-orphaned property.
- **Use the project venv.** `.venv/bin/python` for all Python invocations, per CLAUDE.md.

---

## Resolved Open Items (recorded for context, not action)

These were open items from the ADR-021 planning and implementation discussions; resolved here so the cleanup sessions don't reopen them.

- **Pending_metadata queue triage** (originally OOS item 4 in the implementation tracker): the queue is clear. No triage required as part of cleanup.
- **`force=True` interaction with chain inheritance** (originally OOS item 9 in the implementation tracker): intended behavior is "treat as new ingest" — chain inheritance fires on the force-reingest path. Implementation Chunk 2 will encode this; cleanup does not need to address it.
- **Tags inheritance** (originally OOS item 5 in the implementation tracker): ADR is locked. Tags are not in the chain-inheritance trio; future expansion requires a new ADR amendment, not a cleanup commit.
- **Documentation update timing** (originally OOS item 7 in the implementation tracker): all CAS documentation is stale; refresh occurs as part of the post-beta consolidation effort. Phase C parks the ADR-021-specific revision points until that effort begins.

---

## Session Log

Append a new entry at the end of each Claude Code session that touches this cleanup. Format:

```
### YYYY-MM-DD — Phase X — <status transition>

- What landed (file list or summary)
- What did not land (deferred, blocked, rolled back)
- Verification result (pytest counts, schema validation, anti-coincidental-pass check)
- Open items for next session
- Commit SHA(s)
```

### 2026-05-01 — Tracker created — planning

- Cleanup tracker created at the request of the user, to guide post-implementation sessions.
- Three phases scoped: Phase A (CAS app caller-side coordination, must accompany or precede the SAGE flip), Phase B (vestigial code and configuration removal, independent cleanup), Phase C (CAS documentation refresh, parked for post-beta consolidation).
- Resolved open items recorded: queue is clear; force=True treats as new ingest; tags are locked; docs deferred to post-beta.
- No code landed.
- Next session: ADR-021 implementation Chunk 1 (separate tracker). This cleanup tracker awaits implementation completion (or coordination of Phase A with implementation Chunk 2).

### 2026-05-01 — Phase A — pending → complete

- Landed alongside ADR-021 implementation Chunk 2 (single coordinated change set):
  - `app/backend/ingest_service.py`: `BatchIngestService.run` now passes `needs_review=True` explicitly when constructing the `IngestRequest`. CAS bulk-ingest documents continue to land in the metadata-review queue under the new SAGE default (`needs_review=False`).
  - `tests/app/test_mcp_app_tools.py::test_mcp_013_returns_pending`: rewritten to seed pending state by inserting a `Document` directly via `graph_store.insert_document`. The previous version relied on `sage_ingest` + `metadata_extraction.review_required=True` -- the MCP tool surface does not yet expose `needs_review` (that surface lands in implementation Chunk 4) and `review_required` is now vestigial. The rewrite decouples the test from how a doc becomes unconfirmed and keeps it focused on `sage_pending_metadata` behavior.
- Did not land:
  - All Phase B work (vestigial-field removal); gated on Phase A being stable in main.
  - Frontend audit. `app/src/components/Sidebar.tsx` line 91 still writes `review_required: false` on vault creation; the schema still permits it. Touched in Phase B.
- Verification:
  - Anti-coincidental-pass for the CAS-app change is satisfied implicitly by `tests/app/test_batch_ingest_service.py::test_bis_020_metadata_pending_counts_unconfirmed_only` and the AD021 ingest-pipeline tests: under the new contract, the only path to `metadata_confirmed=False` from `IngestionService.ingest` is `request.needs_review=True`.
  - Full sweep `.venv/bin/python -m pytest tests/sage tests/app` -> 732 passed, 2 unrelated deprecation warnings.
- Open items for Phase B:
  - Live vault YAMLs at `~/sage_vaults/{vault_id}/vault_config.yaml` for `pim_health`, `test_vault`, `theology`, `new_vault` -- audit and remove `metadata_extraction.review_required` once the schema removal is in place.
  - `tests/sage/test_ingestion_metadata_extraction.py::_pim_metadata_extraction` still accepts a `review_required` parameter; vestigial after Chunk 2 (only the `False` path is exercised). Remove during Phase B alongside the schema change.
  - `tests/app/test_app_backend.py::_pim_metadata_extraction` and `tests/app/test_mcp_app_tools.py::_make_vault_config_dict` still write `"review_required": False`. Remove with the schema removal in Phase B.
- Commit SHA(s): not yet committed; staged with the implementation Chunk 1 + 2 changes.

### 2026-05-01 — Phase B — pending → complete

- Schema-first removal of `metadata_extraction.review_required`:
  - `docs/fs/sage/metadata_extraction.schema.json`: removed the `review_required` property and dropped it from the top-level `required` array. The schema now has no required properties; an empty `metadata_extraction: {}` block is valid.
  - `docs/fs/manifest.json`: bumped `substrate_version` 1.11 -> 1.12 with a revision_history entry covering the cleanup.
- SAGE code:
  - `sage/services/ingestion.py`: removed the `self._review_required` member from `__init__`, removed the now-stale comment block describing the field, and trimmed the ADR-021 setter comment to drop the "field is vestigial" note. `IngestionService` no longer reads `review_required` from vault config.
  - `sage/vault_management.py`: removed `"review_required": False,` from the default vault-creation config.
- Frontend:
  - `app/src/components/Sidebar.tsx`: removed `review_required: false,` from the `defaultConfig` payload sent on vault creation. Verified by the test sweep (vault-creation paths in `tests/app/test_mcp_vault_management.py`); not exercised in the dev-server preview because the change is a payload-trim with no rendered surface.
- Tests:
  - Removed `test_ad021_003_vault_review_required_no_longer_consulted` from `tests/sage/test_ad021_ingestion.py` along with its `pim_config_review_required` and `pim_ingestion_service_review_required` fixtures. The premise (vault flag is read but ignored) dissolves once the field can no longer be set. Replaced with a one-paragraph comment marker preserving context. AD021-001 (default skips queue) and AD021-002 (`needs_review=True` enters queue) provide the remaining caller-side coverage.
  - `tests/sage/test_ingestion_metadata_extraction.py`: stripped the `review_required` parameter from `_pim_metadata_extraction` and `_pim_vault_config_dict`; updated the no-pattern fixture to construct an empty `metadata_extraction: {}` block.
  - `tests/sage/conftest.py`, `tests/sage/test_utilities.py` (3 sites), `tests/sage/test_cleanup_refactor.py`, `tests/sage/test_migrate_flag.py`, `tests/sage/test_parse_filename.py`: removed the `review_required` key from `metadata_extraction` dicts.
  - `tests/app/test_app_backend.py`, `tests/app/test_mcp_vault_management.py`, `tests/app/test_sage_api_additions.py`, `tests/app/test_mcp_app_tools.py`: same removal.
- Live vault YAMLs (out-of-repo, operational edits):
  - `~/sage_vaults/pim_health/vault_config.yaml`: removed `review_required: true`.
  - `~/sage_vaults/test_vault/vault_config.yaml`: replaced `review_required: false` with `metadata_extraction: {}`.
  - `~/sage_vaults/theology/vault_config.yaml`, `~/sage_vaults/new_vault/vault_config.yaml`: removed `review_required: false`.
  - Behavioral effect of pim_health change: agent ingests through pim_health that omit `needs_review` will now commit authoritative (`metadata_confirmed=true`) rather than landing in the review queue. This is the designed ADR-021 end-state. CAS UI bulk ingests are unaffected because Phase A passes `needs_review=true` explicitly.
- Verification:
  - JSON Schema meta-validation clean against draft 2020-12.
  - All four live vault YAMLs validate against the updated `metadata_extraction` schema (modulo a pre-existing, unrelated drift in pim_health where `filename_extraction.project_identifier: PIM` is not in the schema's allowlist; same drift exists in `tests/sage/test_ingestion_metadata_extraction.py` test fixtures and predates ADR-021 -- not in Phase B scope).
  - Full sweep `.venv/bin/python -m pytest tests/sage tests/app` -> **736 passed** (predicted 736 = 737 baseline - 1 deleted `test_ad021_003`), 2 unrelated deprecation warnings.
  - Anti-coincidental-pass: schema rejects `{"review_required": false, ...}` with the expected `additionalProperties` error; clean payload and empty-dict payload both validate; live vault YAMLs are confirmed clean post-test (the test suite rewrites `test_vault` and `new_vault` to pytest-tmp storage paths but does not reintroduce `review_required`).
- Out-of-scope finding (flagged, addressed in a follow-up commit same session):
  - `filename_extraction.project_identifier` schema drift -- present in pim_health vault YAML and in the SAGE/app test fixtures, but not in the `filename_extraction` schema's `additionalProperties: false` allowlist. Pre-existing condition; outside ADR-021 scope. Investigation showed the parser actively reads it (sage/services/filename_parser.py:109) and uses it to disambiguate a leading short-uppercase filename segment from a code (lines 168-171); without the field, pim_health's leading "PIM" segment would be misclassified as a code by `^[A-Z][A-Z0-9]{1,7}$`. The CAS Application API spec already references it. Resolution: schema-side gap, not YAML/fixture drift -- added project_identifier to the metadata_extraction schema in a separate substrate-conformance commit (substrate v1.13). Live vault validation now passes for all four vaults.
- Commit SHA(s): not yet committed.

---
