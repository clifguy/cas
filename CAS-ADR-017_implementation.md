# CAS-ADR-017 Implementation Tracker

**ADR:** [CAS-ADR-017](docs/cas_adr_store.json) — Chain-scoped edge resolution with anchor and retraction primitives
**Status:** accepted 2026-04-18
**Plan sequenced:** 2026-04-18
**This file:** transient. Delete when all chunks complete and the final commit lands.

---

## Purpose

This is the cross-session continuity artifact for the ADR-017 implementation. Each Claude Code session working on this implementation reads this file at start and updates it before ending. It encodes: the frozen design decisions, the 8-chunk sequence, per-chunk status, open blockers, and a session log.

The main project tracker (`CAS_Project_Tracker.md`) carries only a one-line status pointer; detail lives here.

---

## Frozen Design Decisions

### Edge-type resolution_policy registry

| Edge type | Resolution policy | Notes |
|---|---|---|
| `supersedes` | `none` | Meta-edge; lineage fact, not propagating relationship |
| `retracts` | `none` | Meta-edge; new in this work |
| `merged_from` | `none` | Meta-edge; new in this work |
| `derived_from` | `transitive_source` | Frozen-at-derivation (patent / USPTO template case) |
| `instantiated_from` | `transitive_both` | **New edge type.** Live-tracking (checklist / template case) |
| `references` | `transitive_both` | Live reference; frozen-citation case would split later if it emerges |
| `covers` | `transitive_both` | Driving case for the ADR |
| `bundles_with` | `transitive_both` | Chain-to-chain bundling |
| `depends_on` | `transitive_both` | Precondition checks resolve to current heads |
| `authoritative_for` | `TBD` | **Deferred.** No concrete use case yet. Write-time error on any attempt to create until declared. |
| `sync_target` | `TBD` | **Deferred.** Possibly deprecate; overlaps with `references` after references became `transitive_both`. Decide before first new use. |

### retracts edge target shape

Use a **separate `retracted_edge_id` column**, not overloading `target_id`. Clean typing, no FK hackery. The `retracts` edge still carries `source_id` (a document on the retracting chain), `retracted_edge_id` (the edge instance being retracted), and its anchor field(s) per the write-time invariant.

### Chain identity

**Implicit** via `document_id` through supersedes lineage. No explicit `chain_id` column. The rare re-pointing case (error recovery) is a documented procedure, not a schema feature.

### merged_from anchoring

Source is the first version of the successor chain; target is the terminal version of each predecessor chain. Chains remain implicit; `merged_from` is an ordinary edge.

---

## 8-Chunk Sequence

Status values: `pending` | `in_progress` | `complete` | `blocked`

### Chunk 1 — FS schema + tier-2 test specs

**Status:** `complete`
**Goal:** Land Formal Substrate changes (EdgeType enum additions including `instantiated_from`, `retracts`, `merged_from`; ResolutionPolicy enum; Edge anchor fields; edge_type_registry schema; `resolution_path` + `debug` on TraverseRequest/Response). Land tier-2 behavioral test specs (`tests/sage/chain_resolution_tests.md`) enumerating ~40-45 TEST-SAGE-CR-NNN cases. No runtime code.
**Critical files:**
- MODIFY `docs/fs/sage/sage_core_api.openapi.yaml`
- CREATE `docs/fs/sage/edge_type_registry.schema.json`
- MODIFY `docs/fs/sage/edge_inference.schema.json`
- MODIFY `docs/fs/manifest.json`
- CREATE `tests/sage/chain_resolution_tests.md`
- MODIFY `tests/sage/contract_tests.md`
- MODIFY `tests/test_plan_manifest.json`

**Dependencies:** none
**Verification:** JSON Schema meta-validation; sample Edge instances pass/fail as expected; manifest integrity tests green
**Blockers:** none remaining

### Chunk 2 — Pydantic derivation + anchor columns + write-time validator

**Status:** `complete`
**Goal:** Derive `Edge`, `EdgeType`, `ResolutionPolicy` Pydantic from FS. SQLite ALTER adds `source_valid_from_version`, `target_valid_from_version`, `valid_until_version`, `retracted_edge_id` nullable columns. `GraphOpsService.link` enforces the policy-keyed write-time invariant. No resolver behavior change.
**Critical files:**
- MODIFY `sage/models/enums.py`
- MODIFY `sage/models/schemas.py`
- CREATE `sage/models/edge_registry.py`
- MODIFY `sage/storage/migrations.py`
- MODIFY `sage/storage/graph_store.py`
- MODIFY `sage/services/graph_ops.py`
- MODIFY `sage/api/errors.py`
- MODIFY `sage/api/routers/graph_ops.py`
- CREATE `tests/sage/test_chain_resolution_schema.py`

**Dependencies:** Chunk 1
**Verification:** Validator unit tests; invalid policy/anchor combinations rejected with 400; existing suite green
**Blockers:** none remaining

### Chunk 3 — Edge anchor backfill migration

**Status:** `complete`
**Goal:** Idempotent migration script backfills anchor fields on existing edges per resolution_policy. Dry-run and partial-reverse flags. `TBD`-policy edges raise an error rather than migrate.
**Critical files:**
- CREATE `scripts/migrate_edge_anchors.py`
- CREATE `tests/sage/test_edge_anchor_migration.py`

**Dependencies:** Chunk 2
**Verification:** Seeded vault with edges across all policies; anchors populated correctly; re-run is no-op; reverse flag restores
**Blockers:** none remaining

### Chunk 4 — sage_traverse honors registry and anchors

**Status:** `complete`
**Goal:** Resolver consults registry and applies anchor-in-lineage filtering during chain walks. Per-request lineage cache. No retraction or tombstone logic yet.
**Critical files:**
- MODIFY `sage/services/graph_ops.py`
- MODIFY `sage/storage/graph_store.py` (add `get_supersedes_lineage`)
- CREATE `tests/sage/test_traverse_anchors.py`

**Dependencies:** Chunks 2, 3
**Verification:** ADR worked example (chains A/B, covers edge at a3/b2): query (a5,b3) surfaces; (a2,b3) does not; (a5,b1) does not. Policy-none edges behave as before.
**Blockers:** Minor open question — behavior when anchor document no longer exists. Recommendation: log warning and treat as "not in lineage" (conservative suppress). Confirm in session.

### Chunk 5 — retracts edge end-to-end

**Status:** `complete`
**Goal:** Creating a `retracts` edge validates `retracted_edge_id` points at a real edge, validates one-sided anchor on retracting chain, assigns `resolution_policy: none`. Resolver short-circuits suppressed edges when retraction is in the queried version's lineage on the retracting chain.
**Critical files:**
- MODIFY `sage/services/graph_ops.py`
- MODIFY `sage/storage/graph_store.py` (add `get_retracts_for_edges` batch lookup)
- MODIFY `sage/models/schemas.py`
- MODIFY `sage/api/errors.py`
- CREATE `tests/sage/test_retracts.py`

**Dependencies:** Chunk 4
**Verification:** ADR worked example: retracts at a7 suppresses covers at (a8,b4), does not suppress at (a6,b3), does not affect query from the counterpart chain.
**Blockers:** none remaining

### Chunk 6 — merged_from + tombstoning

**Status:** `complete`
**Goal:** `merged_from` write path validates chain-terminal/chain-head shape, atomically sets `valid_until_version` on predecessor edges in a single transaction (extends the `supersede_atomic` pattern). Resolver ignores tombstoned edges downstream of termination. Re-attachment tooling is NOT in scope.
**Critical files:**
- MODIFY `sage/services/graph_ops.py`
- MODIFY `sage/storage/graph_store.py` (add `merge_atomic`)
- CREATE `tests/sage/test_merged_from.py`

**Dependencies:** Chunk 5
**Verification:** Chain A merged into C; query from c2 does not find A-B covers edge (no auto-inheritance); historical query at (a8,b4) still surfaces; time-travel query at (a9,b4) succeeds, (c2,b4) fails.
**Blockers:** Open question — precise tombstone predicate ("edges whose relevant anchor is in the terminal predecessor's lineage"). Resolved in session (see 2026-04-18 Chunk 6 entry).

### Chunk 7 — resolution_path debug payload

**Status:** `pending`
**Goal:** Optional `debug: bool = False` on TraverseRequest populates `resolution_path: list[ResolutionPathEntry]` on response. Entries discriminated by `event_type`: `anchor_hit`, `anchor_miss`, `retracts_applied`, `tombstone_applied`. Zero cost when debug disabled (branch-and-skip, not build-then-discard).
**Critical files:**
- MODIFY `sage/models/schemas.py`
- MODIFY `sage/services/graph_ops.py` (thread collector)
- MODIFY `sage/api/routers/graph_ops.py`
- MODIFY `docs/fs/sage/sage_core_api.openapi.yaml` (finalize ResolutionPathEntry schema)
- CREATE `tests/sage/test_resolution_path.py`

**Dependencies:** Chunk 6
**Verification:** Each event_type appears in appropriate scenarios; debug-off path has no payload overhead.
**Blockers:** none remaining

### Chunk 8 — MCP tool schemas + API polish

**Status:** `pending`
**Goal:** MCP tool argument schemas for `sage_link`, `sage_traverse` (and related) expose anchor fields, `retracted_edge_id`, `debug`. OpenAPI documents 400 responses for new error types. Tracker updates.
**Critical files:**
- MODIFY `sage/sage_api_tools.py`
- MODIFY `sage/mcp_server.py`, `sage/mcp_init.py`
- MODIFY `sage/api/routers/graph_ops.py`
- MODIFY `docs/fs/sage/sage_core_api.openapi.yaml`
- MODIFY `CAS_Project_Tracker.md`
- MODIFY `tests/sage/test_mcp_server.py`, `test_mcp_init.py`, `test_api_integration.py`

**Dependencies:** Chunks 2, 5, 6, 7
**Verification:** Full pytest green; MCP round-trip for retracts creation and debug-enabled traverse.
**Blockers:** none remaining

---

## Cross-Cutting Concerns

- **Naming.** Full verbose names throughout: `source_valid_from_version`, `target_valid_from_version`, `valid_until_version`, `retracted_edge_id`, `resolution_policy`.
- **Error taxonomy.** All new errors in `sage/api/errors.py`, extending existing base: `EdgeAnchorPolicyViolationError` (Chunk 2), `RetractTargetNotEdgeError` (Chunk 5), `MergedFromValidationError` (Chunk 6), `TBDPolicyEdgeError` (Chunks 2/3 — attempts to create or migrate edges whose policy is `TBD`).
- **Observability.** Structured log entries at INFO for migration runs; DEBUG for resolver decisions. `resolution_path` field is opt-in.
- **Caching.** Per-request lineage cache lands in Chunk 4 as a request-scoped dict or `LineageCache` abstraction. No process-level caching (ADR-deferred). Encapsulate so Phase 2 can swap.
- **Atomicity.** Both retracts-creation and merge-with-tombstone reuse the existing `supersede_atomic` compound-transaction idiom.
- **FK constraints.** SQLite ALTER ADD COLUMN does not support adding FKs. Enforcement is application-level via the write-time validator.

---

## Out of Scope

1. Process-level caching of chain-head edge sets (ADR-deferred).
2. Chain-merge re-attachment tooling (separate UX work).
3. Supersedes-reassignment error recovery (documented procedure only, no code).
4. `SAGE Architecture Reference.docx` updates (separate artifact work).
5. Formal Substrate reference-document updates (separate artifact work).
6. Dedicated `as_of_version=` parameter on traverse (callers pass anchor as `start_id`).
7. `domains/pim_health/` edge inference config updates for `retracts` and `merged_from`.
8. Migration auto-run on vault init (manual script only).
9. Performance benchmarking harness.
10. Staging-edge anchor carry (staging edges do not gain anchors in this pass).

---

## Session Log

Append a new entry at the end of each Claude Code session that touches this implementation. Format:

```
### YYYY-MM-DD — Chunk N — <status transition>

- What landed (file list or summary)
- What did not land (deferred, blocked, rolled back)
- Open items for next session
- Commit SHA(s)
```

### 2026-04-18 — Planning — plan sequenced, decisions frozen

- Plan produced by Plan agent (8 chunks, cross-cutting concerns, out-of-scope list).
- Registry decisions frozen: `derived_from = transitive_source`, `instantiated_from` (new) and `covers`, `references`, `bundles_with`, `depends_on` all `transitive_both`, meta-edges `none`. `authoritative_for` and `sync_target` deferred with `TBD` policy.
- retracts target shape: separate `retracted_edge_id` column.
- Chain identity: implicit via document_id.
- No code landed. ADR-017 promoted to `accepted` status in `docs/cas_adr_store.json`. `CAS_Project_Tracker.md` updated to v72.
- Next session: begin Chunk 1.

### 2026-04-18 — Chunk 1 — pending → complete

**Landed (FS):**
- `docs/fs/sage/sage_core_api.openapi.yaml`: EdgeType enum extended with `instantiated_from`, `retracts`, `merged_from`. New `ResolutionPolicy` enum. `Edge` schema extended with required `resolution_policy`, nullable `source_valid_from_version`, `target_valid_from_version`, `valid_until_version`, `retracted_edge_id`, and nullable `target_id`. `LinkRequest` extended to match (only `source_id`/`edge_type` required). `TraverseRequest.debug` (bool, default false). `TraverseResponse.resolution_path` (nullable array). New `ResolutionPathEntry` schema with `event_type` discriminator. Link endpoint 400 response documents the four new error codes.
- `docs/fs/sage/edge_type_registry.schema.json`: new schema, 6 contract tests authored against it.
- `docs/fs/sage/edge_inference.schema.json`: edge_type enum extended with the three new types.
- `docs/fs/manifest.json`: registered the new registry schema; substrate_version 1.5 → 1.7 (skips 1.6 which was history-only) with a new revision entry.

**Landed (tests):**
- NEW `tests/sage/chain_resolution_tests.md`: 45 tier-2 behavioral tests (TEST-SAGE-CR-001..045) across six sections (write-time invariant / traverse-honors-policy / retracts / merged_from + tombstoning / resolution_path debug / registry coverage).
- `tests/sage/contract_tests.md`: new `edge_type_registry.schema.json` section (TEST-SAGE-ER-001..006, 6 tests) and new TEST-SAGE-API-005..010 (6 tests) for EdgeType, ResolutionPolicy, Edge anchor shape, LinkRequest shape, TraverseRequest.debug, ResolutionPathEntry. TEST-SAGE-EI-004 constraint list updated. Contract test count 53 → 65.
- `tests/test_plan_manifest.json`: manifest_version 0.9.11 → 0.9.12; chain_resolution_tests.md registered; contract_tests.md test_count updated; revision history entry added.

**Verification:** JSON Schema meta-validation clean; frozen 11-row registry validates; negative instance checks (bad policy, empty entries) reject as expected; OpenAPI structural assertions for EdgeType / ResolutionPolicy enums, Edge anchor fields, LinkRequest shape, TraverseRequest.debug, ResolutionPathEntry event_type all pass.

**Did not land:** no runtime code (by design). Pydantic derivation is Chunk 2.

**Commit SHA(s):** pending user commit.

**Next session:** Chunk 2 — Pydantic derivation from FS, SQLite ALTER for anchor columns, write-time validator in GraphOpsService.link.

### 2026-04-18 — Chunk 2 — pending → complete

**Landed (models):**
- `sage/models/enums.py`: `EdgeType` extended with `INSTANTIATED_FROM`, `RETRACTS`, `MERGED_FROM`. New `ResolutionPolicy` StrEnum (`NONE`, `TRANSITIVE_SOURCE`, `TRANSITIVE_BOTH`, `TBD`).
- `sage/models/schemas.py`: `Edge` gained nullable `resolution_policy`, `source_valid_from_version`, `target_valid_from_version`, `valid_until_version`, `retracted_edge_id`; `target_id` relaxed to nullable. `LinkRequest` gained the same anchor / retracted_edge_id / nullable-target fields. `TraverseRequest.debug` bool. `TraverseResponse.resolution_path` nullable list. New `ResolutionPathEntry` model with `event_type` Literal discriminator.
- `sage/models/edge_registry.py` (NEW): `EdgeTypeRegistry` with built-in default matching the 11-row frozen table (CAS-ADR-017). Vault-config-driven loading is a future chunk per confirmed open question.

**Landed (storage):**
- `sage/storage/migrations.py`: `SCHEMA_VERSION` 1 → 2. Five new `ALTER TABLE edges ADD COLUMN` migrations (all nullable). `EDGES_TABLE` CREATE DDL updated for fresh vaults: `target_id` relaxed to nullable, new columns added inline. No FK on retracted_edge_id (SQLite limitation; enforced at application layer).
- `sage/storage/graph_store.py`: `_exec_insert_edge` writes all five new columns. `_row_to_edge` reads them defensively (row.keys() check to tolerate legacy rows). `ResolutionPolicy` imported.

**Landed (service + errors):**
- `sage/services/graph_ops.py`: `GraphOpsService.__init__` takes optional `edge_type_registry` (defaults to `EdgeTypeRegistry.default()`). `link()` orders checks as source-existence → self-ref → target-existence → policy resolution → TBD rejection → shape validation. Policy is frozen onto the edge row at creation. For `transitive_source`, `target_valid_from_version` is auto-copied from `target_id`. `_validate_link_request_shape` implements the policy-keyed field-shape invariant for all four policies plus the retracts-specific shape (target_id null, retracted_edge_id required, one-sided source anchor).
- `sage/services/lifecycle.py`: both `Edge` constructions (supersede inline and `prepare_supersede`) now set `resolution_policy=ResolutionPolicy.NONE` so atomic-supersede writes carry the frozen policy.
- `sage/api/errors.py`: new `EdgeAnchorPolicyViolationError` (400, `edge_anchor_policy_violation`, with `edge_type`/`resolution_policy`/`violation`/`offending_fields` detail) and `TBDPolicyEdgeError` (400, `tbd_policy_edge`). `RetractTargetNotEdgeError` and `MergedFromValidationError` deferred to Chunks 5 and 6 per tracker.

**Landed (tests):**
- NEW `tests/sage/test_chain_resolution_schema.py`: 12 passing + 2 skipped tests. Passing cover CR-001, CR-002, CR-003, CR-005, CR-006, CR-007, CR-008 (shape only), CR-010, CR-011, CR-012, plus two retracts-shape extras. Skipped: CR-004 (anchor-in-lineage, Chunk 4) and CR-009 (retracted_edge_id existence, Chunk 5), each with a skip-reason pointer.
- `tests/sage/test_graph_ops.py`: BH-031, BH-032, and `test_unlink_deletes_existing_edge` now supply self-anchors (`source_valid_from_version=source_id`, `target_valid_from_version=target_id`) for their REFERENCES edges so they satisfy the transitive_both shape invariant.
- `tests/sage/test_mcp_server.py`: `test_link_creates_edge` and `test_traverse_returns_nodes` switched from REFERENCES to SUPERSEDES (policy `none`) so the MCP wrapper (which does not yet expose anchor params; Chunk 8) still exercises edge creation. `test_link_self_referential_error` unchanged — self-ref check runs before shape validation.
- `tests/sage/test_api_integration.py::test_link_201`: request body now includes `source_valid_from_version` / `target_valid_from_version`.

**Verification:**
- `pytest tests/sage/test_chain_resolution_schema.py`: 12 passed, 2 skipped.
- `pytest tests/sage`: 448 passed, 2 skipped, 0 failed.
- `pytest tests/app`: 139 passed.

**Did not land:**
- Anchor-in-lineage membership check (Chunk 4; `get_supersedes_lineage` not yet present).
- Retracts `retracted_edge_id` existence validation → `RetractTargetNotEdgeError` (Chunk 5).
- Vault-config wiring of the edge-type registry (confirmed acceptable to defer; built-in default serves Chunk 2).
- Backfill of existing edges' `resolution_policy` / anchor columns (Chunk 3).
- MCP tool schema surface for anchor fields (Chunk 8).

**Open items for next session:**
- Chunk 3: backfill script. Existing dev-vault edges carry NULL resolution_policy and NULL anchors; the script populates them per the registry mapping. Also consider whether legacy dev vaults with `target_id NOT NULL` on the edges table need a table rebuild or can be left as-is (new retracts edges are not yet being created and won't be until Chunk 5).

**Commit SHA(s):** pending user commit.

### 2026-04-18 — Chunk 3 — pending → complete

**Landed:**
- NEW `scripts/migrate_edge_anchors.py`: forward-backfill + reverse dev utility. Idempotent selection via `WHERE resolution_policy IS NULL`. Per-policy action: `none` → policy only; `transitive_source` and `transitive_both` → policy + `source_valid_from_version=source_id`, `target_valid_from_version=target_id`. TBD-policy edges halt with exit 2 and zero mutations (even in `--execute` mode); an unknown `edge_type` also halts with exit 2. `--reverse` nulls all five ADR-017 columns across every edge. Dry-run is default; `--execute` applies. Core logic factored into `build_backfill_plan` / `apply_backfill` / `build_reverse_plan` / `apply_reverse` / `run_backfill` / `run_reverse` so both the CLI and tests share the same code path. Vault config discovery mirrors `cleanup_orphan_imports.py`.
- NEW `tests/sage/test_edge_anchor_migration.py`: 9 passing tests. Coverage: per-policy backfill correctness; idempotent re-run; TBD halts without mutation in both dry-run and execute; `apply_backfill` raises RuntimeError directly when handed a plan with TBD entries; dry-run writes nothing; `--reverse` nulls all five columns; reverse dry-run preserves state; mixed prepopulated + legacy rows (pre-populated rows are untouched, legacy rows backfill); per-policy count reporting.

**Verification:**
- `pytest tests/sage/test_edge_anchor_migration.py`: 9 passed.
- `pytest tests/sage`: 457 passed, 2 skipped, 0 failed (up from 448 passed, +9 new tests).

**Anchor-choice rationale (design note for later chunks):** legacy `transitive_source` and `transitive_both` edges both backfill their anchors to `source_id` / `target_id`. This asserts "this edge has been in force from the respective endpoint's own version forward," which is the most conservative reconstruction for edges created before the anchor field existed. The resolver (Chunk 4) will read these anchors via registry + lineage accessor; the backfilled values place legacy edges in the lineage of their endpoints without additional metadata.

**Did not land:**
- `test_plan_manifest.json` registration: this is a script-level test and the tier-2 `chain_resolution_tests.md` plan does not include migration-script cases. Flagged during proposal; not adopted. Revisit if script tests should have a dedicated manifest entry.
- Auto-run on vault init (explicit out-of-scope item 8).

**Open items for next session:**
- Chunk 4: `sage_traverse` consults the registry and applies anchor-in-lineage filtering. Requires `get_supersedes_lineage` on `GraphStore` and a per-request lineage cache. Open question carried forward from tracker: behavior when an anchor document no longer exists — ADR recommendation is log warning + conservative suppress ("not in lineage").

**Commit SHA(s):** pending user commit.

### 2026-04-18 — Chunk 4 — pending → complete

**Landed (storage):**
- `sage/storage/graph_store.py`: new `get_supersedes_lineage(doc_id)` returning doc_id and all supersedes-predecessors via recursive CTE (newest first). Empty list when doc_id missing (caller interprets as "not in lineage"). Existing `_traverse_sync` CTE extended: seed and recursive-step SELECTs now carry `resolution_policy`, `source_valid_from_version`, `target_valid_from_version`; row dict surfaces the same three columns downstream.

**Landed (service + filter):**
- `sage/services/graph_ops.py`: new `_LineageCache` class — per-request `dict[str, frozenset[str]]` with a `fetches` counter for test instrumentation (CR-022). `GraphOpsService.traverse` now:
  - Allocates one `_LineageCache` per request.
  - Splits `direction=both` into outbound and inbound phases so each phase can carry its own seed set.
  - Computes seeds via new `_determine_seeds`: `policy=none` → `[start_id]`; `transitive_source+inbound` → `[start_id]` (target frozen); `transitive_source+outbound` and all `transitive_both` → `start_lineage`; no edge_type filter → `start_lineage` (most permissive, per-edge filter cuts false positives).
  - Runs the existing `graph_store.traverse` per seed and merges by `edge_id`, keeping min depth.
  - Applies `_edge_passes_anchor_filter` per row: stored `resolution_policy` is authoritative (frozen at creation); legacy rows without a stored policy fall through to the registry. `policy=none` passes; `TBD` logs WARN and suppresses; `transitive_source` requires `source_valid_from_version ∈ lineage(source_id)`; `transitive_both` additionally requires `target_valid_from_version ∈ lineage(target_id)`.
  - Missing anchor document path (CR-021) logs WARN via `logger = logging.getLogger(__name__)` and returns `False` (conservative suppress, deterministic non-500 result).
- `sage/services/graph_ops.py::link`: new `_validate_anchor_in_lineage` runs after shape validation. For `transitive_source` it enforces `source_valid_from_version ∈ lineage(source_id)`; for `transitive_both` it enforces both anchors are in their endpoint lineages. For `retracts` (policy `none`) with a source anchor, it enforces the anchor is in `source_id`'s lineage on the retracting chain. Missing anchor documents surface as `EdgeAnchorPolicyViolationError`, matching the contract CR-004 exercises.

**Landed (tests):**
- NEW `tests/sage/test_traverse_anchors.py`: 10 passing tests (CR-013..CR-022). Worked-example helpers (`_seed_supersedes_chain`, `_seed_ab_worked_example`) assemble the canonical chains A/B + covers edge. Covers: hit from (a5,b3) both directions, suppressed from (a2,b3) and (_, b1), inclusive anchor match at (a3,b2), `supersedes` ignores anchor logic, `derived_from` (transitive_source) source-anchor-in-lineage hit, target-chain-advance frozen behavior, mixed traverse per-edge policy, missing-anchor WARN + suppress, per-request lineage cache bounded by distinct endpoint count.
- `tests/sage/test_chain_resolution_schema.py`: `CR-004` unskipped and implemented end-to-end — builds a 3-node supersedes chain (a3 ← a4 ← a5), attempts to create a `covers` edge from a3 with `source_valid_from_version=a5` (a descendant, not an ancestor), expects `EdgeAnchorPolicyViolationError` with `source_valid_from_version` in `offending_fields`. Added `Edge` to imports.
- `tests/sage/test_graph_ops.py`: BH-037, BH-097, BH-098, BH-100 were directly inserting `references`/`covers` edges via `graph_store.insert_edge` with no `resolution_policy` or anchors; under the new filter those rows now correctly drop out. Updated each to construct `Edge(..., resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH, source_valid_from_version=<source_id>, target_valid_from_version=<target_id>)` to mirror what `link()` writes. Imported `ResolutionPolicy`.

**Verification:**
- `pytest tests/sage/test_traverse_anchors.py tests/sage/test_chain_resolution_schema.py`: 23 passed, 1 skipped (CR-009 owned by Chunk 5).
- `pytest tests/sage`: 468 passed, 1 skipped, 0 failed (up from 457 passed / 2 skipped; +11 net passes: 10 new traverse tests + CR-004 now passing; the other Chunk-2 skip is still CR-009).
- `pytest tests/app`: 139 passed.

**Design notes:**
- Direction-split-by-phase is deliberate: for `transitive_source` the target is frozen, so an inbound query must seed exactly at `start_id` (not its lineage) to avoid surfacing edges that target an ancestor. Merging `direction=both` into two per-phase calls lets each phase carry its own seed rule cleanly.
- The stored `resolution_policy` on each edge row wins over the registry when they disagree. This protects already-created edges from a later registry edit (CR-012 invariant) and is the reason the filter consults `row.get("resolution_policy")` first.
- The mixed-traverse case (no `edge_type` filter) is handled conservatively: expand to `start_lineage` for both phases and let per-edge filtering prune. This over-seeds on `transitive_source` inbound but the per-edge filter catches it; a stricter per-type split is not required until a concrete downstream need surfaces.

**Did not land:**
- `retracts` suppression (Chunk 5).
- `merged_from` tombstoning (Chunk 6).
- `resolution_path` debug events — `TraverseResponse.resolution_path` remains `None` (Chunk 7).
- Vault-config-driven registry load (confirmed deferrable; built-in default still serves).

**Open items for next session:**
- Chunk 5: `retracts` end-to-end — validate `retracted_edge_id` points at a real edge (unskips CR-009), one-sided source-anchor shape (shape check already lands in Chunk 2), and resolver short-circuits suppressed edges when a retraction is in the queried version's lineage on the retracting chain. The anchor-in-lineage write-time path already covers the retracting-chain anchor check; Chunk 5 adds the referenced-edge-existence check and the resolver hook.

**Commit SHA(s):** pending user commit.

### 2026-04-18 — Chunk 5 — pending → complete

**Landed (errors + service):**
- `sage/api/errors.py`: new `RetractTargetNotEdgeError` (400, `retract_target_not_edge`, detail `{retracted_edge_id}`).
- `sage/services/graph_ops.py::link`: after shape validation (and before anchor-in-lineage checks), `retracts` edges now call `graph_store.get_edge(retracted_edge_id)` and raise `RetractTargetNotEdgeError` when the referenced edge does not exist. Placement preserves existing error ordering: shape (missing field) errors still surface before existence errors.
- `sage/services/graph_ops.py::traverse`: new `_apply_retracts` stage runs after `_edge_passes_anchor_filter` and before dedup. For rows whose effective policy is `transitive_source` or `transitive_both`, candidate `edge_id`s are batch-looked up via `graph_store.get_retracts_for_edges`. A retracts edge suppresses its target iff `retracts.source_valid_from_version ∈ lineage(request.start_id)`. Policy=none edges (supersedes, merged_from, and retracts itself) are deliberately exempt per CR-028 ("retracts is a primitive, not a veto gate"). New helper `_effective_policy` centralizes the stored-wins-over-registry rule used by both the anchor filter and the retracts stage.

**Landed (storage):**
- `sage/storage/graph_store.py`: new `get_retracts_for_edges(edge_ids)` returning `{retracted_edge_id: [Edge, ...]}` via a single `WHERE edge_type='retracts' AND retracted_edge_id IN (...)` query. Omits edges with no retractions. Handles empty input.

**Landed (tests):**
- NEW `tests/sage/test_retracts.py`: 6 passing tests (CR-023..CR-028). Shared `_seed_retract_setup` helper assembles the canonical ADR worked example extended to `a8` plus a retracts edge anchored at a7. Coverage: suppress downstream of retract anchor (CR-023), no-suppress upstream of anchor (CR-024), one-sided (counterpart chain unaffected) (CR-025), multiple retracts — any in-lineage suppresses (CR-026), retracts edge itself introspectable via storage layer (CR-027), retracts of a policy=none edge does not rewrite the chain (CR-028).
- `tests/sage/test_chain_resolution_schema.py`: `test_cr_009_retracts_unknown_target_edge` unskipped and implemented — a valid-shaped retracts with `retracted_edge_id='does-not-exist'` now raises `RetractTargetNotEdgeError(status_code=400, code='retract_target_not_edge', detail['retracted_edge_id']=...)`. `test_cr_008_retracts_shape_accepted` adapted to seed a real `covers` edge first so the existence check passes.

**Verification:**
- `pytest tests/sage/test_retracts.py tests/sage/test_chain_resolution_schema.py`: 20 passed (was 12 passed + 2 skipped on CR-009/CR-004; now both are active, plus 6 new retracts tests).
- `pytest tests/sage`: 475 passed, 0 skipped, 0 failed (up from 468 passed / 1 skipped; net +7 tests: 6 new CR-023..028 + CR-009 unskipped; prior skipped CR-009 slot is now a passing test).

**Design notes:**
- CR-027 asserts the retracts edge is introspectable but uses `get_edges_by_source` rather than the document-oriented `traverse` API. The traverse path joins on documents via `target_id`, and retracts edges carry `target_id=null`; surfacing them through traverse would require a companion lookup. Out of scope for Chunk 5 and not required by the tier-2 test (introspection intent is satisfied).
- The retracts-suppression predicate uses only `lineage(request.start_id)`, not per-seed lineage. This is intentional: the user's query is from a specific version; retraction semantics are defined against that version's knowledge state. Per-seed application would conflate the traversal's intermediate frontier with the user's query context.
- The stored-policy-wins-over-registry rule (CR-012) now has a shared implementation (`_effective_policy`) used by both the anchor filter and the retracts stage. Chunks 6 and 7 will reuse it for tombstoning and debug-path events respectively.

**Did not land:**
- merged_from write path and tombstoning (Chunk 6).
- resolution_path debug events for `retracts_applied` (Chunk 7 — the resolver stage is present, but no event emission yet).
- MCP tool schemas for `retracted_edge_id` and anchor fields (Chunk 8).

**Open items for next session:**
- Chunk 6: `merged_from` end-to-end. Write path validates chain-terminal predecessor / chain-head successor, and atomically sets `valid_until_version` on predecessor-downstream edges in a single transaction (extend the `supersede_atomic` pattern). Resolver ignores tombstoned edges when the queried version is downstream of the termination on the predecessor chain. Open question (from the plan): precise tombstone predicate — "edges whose relevant anchor is in the terminal predecessor's lineage" — needs confirmation before writing the traversal filter. Also requires a new error `MergedFromValidationError` (400) and a new storage primitive `merge_atomic` beside `supersede_atomic`.

**Commit SHA(s):** pending user commit.

### 2026-04-18 — Chunk 6 — pending → complete

**Tombstone predicate resolution (open question closed):**
- **Write-time scope:** when `merged_from(source=successor_first, target=predecessor_terminal)` is created, in the same transaction set `valid_until_version = predecessor_terminal` on every edge whose `source_id OR target_id ∈ lineage(predecessor_terminal)` AND `resolution_policy != 'none'` AND `valid_until_version IS NULL`. Policy-none edges (supersedes, retracts, merged_from itself) are exempt so the predecessor chain's lineage and meta-facts stay navigable. Endpoint membership is equivalent to anchor membership here because the write-time anchor-in-lineage invariant guarantees each anchor sits inside its endpoint's lineage, so if the endpoint is on the predecessor chain the anchor is too.
- **Resolver filter:** a row is suppressed iff `valid_until_version is not None` AND `valid_until_version ∈ lineage(query_start_id)` AND `valid_until_version != query_start_id`. The strict-ancestor arm means query at the termination version itself still surfaces the edge (CR-034); query at a pre-merge version finds the tombstone out of its lineage and keeps the edge (CR-035); a hypothetical post-merge version on the predecessor chain finds the tombstone as a strict ancestor and suppresses.

**Landed (errors):**
- `sage/api/errors.py`: new `MergedFromValidationError` (400, `merged_from_validation`, detail `{violation, source_id?, target_id?}`).

**Landed (storage):**
- `sage/storage/graph_store.py`: three new primitives — `has_supersedes_successor(doc_id)` (True if anything supersedes doc_id; used to reject non-head predecessors), `has_supersedes_predecessor(doc_id)` (True if doc_id supersedes something; used to reject non-first successors), `find_tombstone_candidates(lineage_ids)` (returns non-policy-none, not-yet-tombstoned edge ids whose source_id or target_id is in the lineage set), and `merge_atomic(merged_from_edge, tombstone_edge_ids, tombstone_version)` (single transaction: insert merged_from edge + UPDATE `valid_until_version` on the tombstone set). Existing `_traverse_sync` CTE SELECT / row-dict extended with `valid_until_version`.
- Tombstone candidate SQL explicitly excludes `resolution_policy = 'none'` (supersedes, retracts, merged_from) so lineage and meta-facts remain navigable post-merge.

**Landed (service):**
- `sage/services/graph_ops.py::link`: routes `merged_from` through `_validate_merged_from_chain_positions` after shape validation (which already rejects anchor fields on policy-none edges, covering CR-036). For `merged_from`, computes `lineage(target_id)`, finds tombstone candidates, and calls `merge_atomic` — the single-transaction write path substitutes for the normal `insert_edge` call.
- `sage/services/graph_ops.py::traverse`: new `_apply_tombstones` stage runs after `_apply_retracts` and before dedup. Implements the strict-ancestor predicate above. Includes a fast-path early return when no row in the batch has a non-null `valid_until_version`.

**Landed (tests):**
- NEW `tests/sage/test_merged_from.py`: 9 passing tests. Shared `_seed_ab_worked_example` helper assembles Chain A (a1..a8), Chain B (b1..b4), and the canonical covers edge. Coverage: CR-029 valid happy path (201 + persistence), CR-030 non-terminal predecessor rejected, CR-031 non-first successor rejected, CR-032 atomic tombstoning (covers gets `valid_until_version=a8`, supersedes edges within Chain A untouched, merged_from itself untouched), CR-033 no auto-inheritance at c2 post-merge, CR-034 query at merge boundary surfaces, CR-035 time-travel pre-merge surfaces, CR-036 anchor fields rejected with `EDGE_ANCHOR_POLICY_VIOLATION`, plus an extra `test_tombstone_suppresses_strict_downstream_version` confirming the strict-ancestor arm fires for a hypothetical a9 appended after the merge.

**Verification:**
- `pytest tests/sage/test_merged_from.py`: 9 passed.
- `pytest tests/sage`: 484 passed, 0 skipped, 0 failed (up from 475 passed; +9 new tests, no regressions in adjacent suites).

**Design notes:**
- The strict-ancestor arm (`valid_until_version != query_start_id`) is the load-bearing detail separating CR-034 from the "hypothetical a9" suppression case. An inclusive check would break CR-034. A strictly-descendant check (requires a DAG walk from the tombstone) would be redundant because `lineage(query_start_id)` already encodes descendant-ness for the supersedes graph.
- Tombstone candidate selection uses endpoint membership (source_id / target_id) rather than anchor membership. These are equivalent given the write-time anchor-in-lineage invariant enforced by `_validate_anchor_in_lineage`, and endpoint membership is cheaper to index/query. If a future relaxation allows anchors outside their endpoint's lineage, the selection will need to move to anchor membership.
- Policy-none edges (supersedes, retracts, merged_from) are structurally exempt from tombstoning because tombstoning them would break the supersedes CTE used for lineage reconstruction and would invalidate audit queries that walk retracts and merged_from lineage facts. The exemption is explicit in the SQL.
- Symmetric helpers `has_supersedes_predecessor` / `has_supersedes_successor` are named against the supersedes direction (source=newer supersedes target=older), not calendar time. `has_supersedes_predecessor(c2)` is True when c2 supersedes something (c2 has an older predecessor), which is exactly the "not first" condition for CR-031. Keeping the names tied to edge direction avoids the "chain first vs chain tail" terminology trap in the ADR.

**Did not land:**
- `resolution_path` debug events for `tombstone_applied` (Chunk 7 — resolver stage is in place, event emission is not).
- MCP tool schemas for `merged_from` surface (Chunk 8).
- Chain-merge re-attachment tooling (explicitly out-of-scope item 2).

**Open items for next session:**
- Chunk 7: `resolution_path` debug payload. Thread a collector through `traverse` / `_edge_passes_anchor_filter` / `_apply_retracts` / `_apply_tombstones` that emits `anchor_hit`, `anchor_miss`, `retracts_applied`, `tombstone_applied` entries. Request field already wired in Chunk 1; response field already present. Must be zero cost when `debug=False` (branch-and-skip, not build-then-discard). Finalize `ResolutionPathEntry` schema if Chunk 1's version needs more fields to cover the four event types cleanly.

**Commit SHA(s):** pending user commit.
