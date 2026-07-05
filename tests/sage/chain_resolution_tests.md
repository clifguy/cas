# SAGE Chain Resolution Tests

Tier 2 behavioral tests for chain-scoped edge resolution with anchor and
retraction primitives (CAS-ADR-017). Tests encode the design decisions
frozen in `CAS-ADR-017_implementation.md`: the edge-type `resolution_policy`
registry, anchor columns (`source_valid_from_version`,
`target_valid_from_version`, `valid_until_version`), the `retracts` and
`merged_from` meta-edges, and the `resolution_path` debug payload on
`traverse`.

**Test ID prefix:** `TEST-SAGE-CR-NNN`.

**Worked example used throughout.** Two chains:

- Chain A: `a1 ← a2 ← a3 ← a4 ← a5 ← a6 ← a7 ← a8` (supersedes, each pointing to its predecessor)
- Chain B: `b1 ← b2 ← b3 ← b4`

A `covers` edge is created with `source_id = a3`, `target_id = b2`,
`source_valid_from_version = a3`, `target_valid_from_version = b2`. This is
the canonical setup for anchor-in-lineage tests; deviations are noted per
test.

**Frozen registry used throughout (derived from CAS-ADR-017):**

| edge_type | resolution_policy |
|---|---|
| supersedes | none |
| retracts | none |
| merged_from | none |
| derived_from | transitive_source |
| instantiated_from | transitive_both |
| references | transitive_both |
| covers | transitive_both |
| bundles_with | transitive_both |
| depends_on | transitive_both |
| authoritative_for | TBD |
| sync_target | TBD |

---

## Section 1 — Write-time invariant enforcement

Tests here create edges via `POST /sage_vaults/{vault_id}/edges` (or its
internal `GraphOpsService.link` equivalent) and assert the policy-keyed
anchor invariant. Valid cases produce 201 with the created edge; invalid
cases produce 400 with the documented error code.

### TEST-SAGE-CR-001: transitive_both edge with both anchors in lineage — accepted

**Artifact:** `sage/services/graph_ops.py` (write-time validator)
**Category:** valid
**Policy:** `transitive_both`

**Precondition:** Chains A and B exist.

**Input:** `link(source_id=a3, target_id=b2, edge_type=covers, source_valid_from_version=a3, target_valid_from_version=b2)`

**Expected:** 201. The created Edge carries `resolution_policy: transitive_both` and both anchor fields populated as supplied.

**Rationale:** Canonical happy path for `covers` and the other `transitive_both` edge types.

### TEST-SAGE-CR-002: transitive_both edge with missing source anchor — rejected

**Category:** invalid
**Policy:** `transitive_both`

**Input:** `link(source_id=a3, target_id=b2, edge_type=covers, target_valid_from_version=b2)` (no `source_valid_from_version`).

**Expected:** 400 `EDGE_ANCHOR_POLICY_VIOLATION`. No edge created.

### TEST-SAGE-CR-003: transitive_both edge with missing target anchor — rejected

**Category:** invalid
**Policy:** `transitive_both`

**Input:** `link(source_id=a3, target_id=b2, edge_type=covers, source_valid_from_version=a3)` (no `target_valid_from_version`).

**Expected:** 400 `EDGE_ANCHOR_POLICY_VIOLATION`.

### TEST-SAGE-CR-004: transitive_both edge with source anchor outside source chain lineage — rejected

**Category:** invalid
**Policy:** `transitive_both`

**Input:** `link(source_id=a3, target_id=b2, edge_type=covers, source_valid_from_version=b1, target_valid_from_version=b2)` (source anchor points at a document not in the supersedes lineage of a3).

**Expected:** 400 `EDGE_ANCHOR_POLICY_VIOLATION`.

### TEST-SAGE-CR-005: transitive_source edge with source anchor supplied — accepted, target anchor stored as null

**Category:** valid
**Policy:** `transitive_source`

**Input:** `link(source_id=report_v3, target_id=uspto_template_v2, edge_type=derived_from, source_valid_from_version=report_v3)`

**Expected:** 201. Created Edge carries `resolution_policy: transitive_source`, `source_valid_from_version: report_v3`, `target_valid_from_version: null`. Target anchor is inapplicable for this policy (target is frozen at derivation; version specificity is already carried by `target_id`). Standard null-means-not-applicable semantics per CAS-ADR-017.

### TEST-SAGE-CR-006: transitive_source edge with target anchor explicitly supplied — rejected

**Category:** invalid
**Policy:** `transitive_source`

**Input:** `link(source_id=report_v3, target_id=uspto_template_v2, edge_type=derived_from, source_valid_from_version=report_v3, target_valid_from_version=uspto_template_v1)` (caller tried to set target anchor away from target_id).

**Expected:** 400 `EDGE_ANCHOR_POLICY_VIOLATION`. Target anchor is frozen at derivation and must equal target_id; callers may not supply a different value.

### TEST-SAGE-CR-007: policy=none edge (supersedes) with anchors supplied — rejected

**Category:** invalid
**Policy:** `none`

**Input:** `link(source_id=a5, target_id=a4, edge_type=supersedes, source_valid_from_version=a5)`

**Expected:** 400 `EDGE_ANCHOR_POLICY_VIOLATION`. Non-retracts policy-none edges must not carry anchor fields.

### TEST-SAGE-CR-008: retracts edge with retracted_edge_id and one-sided source anchor — accepted

**Category:** valid
**Policy:** `none` (retracts)

**Precondition:** A `covers` edge E with id=`edge-covers-a3-b2` exists.

**Input:** `link(source_id=a7, edge_type=retracts, retracted_edge_id=edge-covers-a3-b2, source_valid_from_version=a7)` (no target_id).

**Expected:** 201. Created Edge has `target_id: null`, `retracted_edge_id: edge-covers-a3-b2`, `source_valid_from_version: a7`, `target_valid_from_version: null`, `resolution_policy: none`.

### TEST-SAGE-CR-009: retracts edge with unknown retracted_edge_id — rejected

**Category:** invalid
**Policy:** `none` (retracts)

**Input:** `link(source_id=a7, edge_type=retracts, retracted_edge_id=does-not-exist, source_valid_from_version=a7)`

**Expected:** 400 `RETRACT_TARGET_NOT_EDGE`.

### TEST-SAGE-CR-010: TBD-policy edge creation — rejected

**Category:** invalid
**Policy:** `TBD`

**Input:** `link(source_id=d1, target_id=d2, edge_type=authoritative_for, source_valid_from_version=d1, target_valid_from_version=d2)`

**Expected:** 400 `TBD_POLICY_EDGE`. The registry marks `authoritative_for` as TBD; creation is blocked until the policy is frozen.

### TEST-SAGE-CR-011: non-retracts edge with target_id omitted — rejected

**Category:** invalid

**Input:** `link(source_id=a3, edge_type=covers, source_valid_from_version=a3)` (no target_id, not a retracts edge).

**Expected:** 400 `EDGE_ANCHOR_POLICY_VIOLATION` (or the existing missing-field error). Only `retracts` edges may omit target_id.

### TEST-SAGE-CR-012: resolution_policy field frozen at creation despite later registry edit

**Category:** invariant

**Precondition:** Registry sets `covers` → `transitive_both`. A `covers` edge is created.

**Input:** Registry is edited to change `covers` → `transitive_source` (hypothetical admin operation). The previously-created edge row is re-read.

**Expected:** The existing edge's `resolution_policy` field still reads `transitive_both`. Per-edge policy is copied at creation and not re-derived from the registry on read.

---

## Section 2 — Traverse honors policy + anchors

All tests in this section use the canonical worked example (Chain A,
Chain B, `covers` edge anchored at a3/b2) unless otherwise noted. They
exercise `traverse` after Chunk 4 lands. No retraction or tombstoning
is in play here.

### TEST-SAGE-CR-013: query from (a5, b3) surfaces the covers edge

**Category:** resolution
**Policy:** `transitive_both`

**Input:** `traverse(start_id=a5, edge_type=covers, direction=outbound, depth=2)`, then separately `traverse(start_id=b3, edge_type=covers, direction=inbound, depth=2)`.

**Expected:** Both traversals surface the `covers` edge.

**Rationale:** The source anchor a3 is in a5's supersedes lineage, and the target anchor b2 is in b3's supersedes lineage. Per ADR-017, this is a hit.

### TEST-SAGE-CR-014: query from (a2, b3) suppresses the covers edge

**Category:** resolution
**Policy:** `transitive_both`

**Input:** `traverse(start_id=a2, edge_type=covers, direction=outbound, depth=2)`.

**Expected:** `covers` edge is NOT surfaced (anchor a3 is not in a2's lineage; a2 is earlier on the chain).

### TEST-SAGE-CR-015: query from (a5, b1) suppresses the covers edge

**Category:** resolution
**Policy:** `transitive_both`

**Input:** `traverse(start_id=b1, edge_type=covers, direction=inbound, depth=2)`.

**Expected:** `covers` edge is NOT surfaced (target anchor b2 is not in b1's lineage; b1 is earlier on Chain B).

### TEST-SAGE-CR-016: query from (a3, b2) — anchors exactly match — surfaces the edge

**Category:** resolution
**Policy:** `transitive_both`

**Input:** `traverse(start_id=a3, edge_type=covers, direction=outbound, depth=2)`.

**Expected:** `covers` edge surfaces. Anchor-in-lineage check is inclusive (anchor = start document is a hit).

### TEST-SAGE-CR-017: policy=none edge (supersedes) traverses without anchor filtering

**Category:** resolution
**Policy:** `none`

**Input:** `traverse(start_id=a5, edge_type=supersedes, direction=outbound, depth=5)`.

**Expected:** The full supersedes chain walk (a5 → a4 → a3 → a2 → a1) is returned, unaffected by any anchor logic. Policy `none` short-circuits lineage filtering.

### TEST-SAGE-CR-018: transitive_source edge from report_v4 — source anchor in lineage — surfaces

**Category:** resolution
**Policy:** `transitive_source`

**Precondition:** Report chain `report_v1 ← v2 ← v3 ← v4`. `derived_from` edge with `source_id=report_v3, target_id=uspto_template_v2, source_valid_from_version=report_v3, target_valid_from_version=uspto_template_v2`.

**Input:** `traverse(start_id=report_v4, edge_type=derived_from, direction=outbound, depth=1)`.

**Expected:** Edge surfaces. Source anchor is in v4's lineage; target is frozen (not re-checked).

### TEST-SAGE-CR-019: transitive_source edge — target chain head advances — edge still points at frozen target

**Category:** resolution
**Policy:** `transitive_source`

**Precondition:** Same as CR-018. Then a new USPTO template version `uspto_template_v3` is added on the template chain (supersedes v2).

**Input:** `traverse(start_id=report_v4, edge_type=derived_from, direction=outbound, depth=1)`.

**Expected:** Edge surfaces with `target_id = uspto_template_v2` (the frozen derivation source). The resolver does NOT re-point to the current template chain head.

### TEST-SAGE-CR-020: mixed traverse without edge_type filter honors per-edge policy

**Category:** resolution

**Input:** `traverse(start_id=a2, direction=outbound, depth=3)` (no edge_type filter). Assume a2 also has a `supersedes` edge.

**Expected:** The supersedes edge surfaces (policy none, no filtering); the `covers` edge does NOT surface (policy transitive_both, anchor a3 not in a2's lineage). Per-edge policy is applied individually.

### TEST-SAGE-CR-021: traverse to anchor document that no longer exists — conservative suppress

**Category:** resolution
**Policy:** `transitive_both`
**Open question resolution:** per Chunk 4 blocker note, treat as "not in lineage" with a warning.

**Precondition:** `covers` edge anchored at a3/b2. Document a3 is purged (hypothetical repair scenario).

**Input:** `traverse(start_id=a5, edge_type=covers, direction=outbound, depth=2)`.

**Expected:** `covers` edge is NOT surfaced. A warning is logged at WARN level identifying the missing anchor document. Result is deterministic (not a 500).

### TEST-SAGE-CR-022: per-request lineage cache — repeated lineage lookups within one request coalesce

**Category:** performance / correctness

**Input:** `traverse(start_id=a5, depth=5)` over a graph with 10+ edges all anchored at various points on Chain A. Instrument the graph store to count `get_supersedes_lineage` calls.

**Expected:** Lineage for a5 is fetched at most once during the request. Subsequent anchor checks for other edges on Chain A reuse the cached lineage. No process-level cache persists after the request.

---

## Section 3 — retracts edge end-to-end

### TEST-SAGE-CR-023: retracts at a7 suppresses covers at query (a8, *)

**Category:** resolution
**Policy:** `none` (retracts)

**Precondition:** Canonical setup plus a `retracts` edge with `source_id=a7, retracted_edge_id=<edge-covers-a3-b2>, source_valid_from_version=a7`.

**Input:** `traverse(start_id=a8, edge_type=covers, direction=outbound, depth=2)`.

**Expected:** The `covers` edge is NOT surfaced. Retraction anchor a7 is in a8's supersedes lineage on the retracting (Chain A) side.

### TEST-SAGE-CR-024: retracts at a7 does not suppress covers at query (a6, b3)

**Category:** resolution

**Input:** `traverse(start_id=a6, edge_type=covers, direction=outbound, depth=2)`.

**Expected:** The `covers` edge surfaces. Retraction anchor a7 is NOT in a6's lineage (a6 precedes a7 on Chain A).

### TEST-SAGE-CR-025: retracts does not affect queries from the counterpart chain

**Category:** resolution

**Input:** `traverse(start_id=b3, edge_type=covers, direction=inbound, depth=2)`.

**Expected:** The `covers` edge surfaces. Retracts anchoring is one-sided on the retracting chain; Chain B queries are unaffected.

### TEST-SAGE-CR-026: multiple retracts of the same edge — first in lineage wins

**Category:** resolution

**Precondition:** Canonical setup. Two `retracts` edges for the same `covers` edge: one anchored at a5, one at a7.

**Input:** `traverse(start_id=a8, edge_type=covers, direction=outbound, depth=2)`, `traverse(start_id=a6, edge_type=covers, direction=outbound, depth=2)`, `traverse(start_id=a4, edge_type=covers, direction=outbound, depth=2)`.

**Expected:** `covers` is suppressed at a8 and a6 (both have a5 in lineage); surfaces at a4 (neither retract anchor is in a4's lineage). Resolver short-circuits on the first in-lineage retraction.

### TEST-SAGE-CR-027: retracts edge itself is traversable as a supersedes-style lineage fact

**Category:** introspection

**Input:** `traverse(start_id=a7, edge_type=retracts, direction=outbound, depth=1)`.

**Expected:** The `retracts` edge surfaces in the traversal, since its policy is `none` (no anchor filtering for the meta-edge itself). Enables auditors to list all retractions on a chain.

### TEST-SAGE-CR-028: retracts of a policy=none edge — permitted, lineage fact only

**Category:** edge case

**Input:** Create a `retracts` edge targeting a `supersedes` edge (hypothetical recovery from an erroneous supersession). Then traverse supersedes.

**Expected:** The retracts edge is created (no write-time block). Traversal of supersedes is unaffected (policy none ignores retracts suppression by design: retracts only short-circuits edges whose resolution would otherwise apply).
**Rationale:** Documents that retracts is a primitive, not a veto gate on all edges indiscriminately. Retracting a supersedes edge is a lineage annotation, not a chain rewrite.

---

## Section 4 — merged_from + tombstoning

### TEST-SAGE-CR-029: merged_from write path requires chain-terminal predecessor, chain-head successor

**Category:** invariant
**Policy:** `none` (merged_from) write-time invariant

**Precondition:** Chain A head is a8 (no successor). Chain C head is c2 (no successor).

**Input:** `link(source_id=c1, target_id=a8, edge_type=merged_from)` where c1 is the FIRST version of Chain C (the successor chain) and a8 is the TERMINAL version of Chain A (the predecessor chain).

**Expected:** 201. Edge created. Policy `none`; no anchor fields set.

### TEST-SAGE-CR-030: merged_from with non-terminal predecessor — rejected

**Category:** invalid

**Input:** `link(source_id=c1, target_id=a5, edge_type=merged_from)` where a5 is a mid-chain version.

**Expected:** 400 `MERGED_FROM_VALIDATION`.

### TEST-SAGE-CR-031: merged_from with non-head successor — rejected

**Category:** invalid

**Input:** `link(source_id=c2, target_id=a8, edge_type=merged_from)` (c2 is not the first version of Chain C).

**Expected:** 400 `MERGED_FROM_VALIDATION`.

### TEST-SAGE-CR-032: merged_from atomically tombstones predecessor chain's downstream edges

**Category:** atomicity

**Precondition:** Canonical setup (covers edge anchored a3/b2 on Chain A × Chain B). Chain C with single document c1 is added. The `covers` edge has `valid_until_version: null` before the merge.

**Input:** `link(source_id=c1, target_id=a8, edge_type=merged_from)`.

**Expected:** In a single database transaction: the `merged_from` edge is created AND the `covers` edge (and any other predecessor-downstream edges) has `valid_until_version` set to a8. If the transaction is aborted mid-operation (test injection), neither change persists.

### TEST-SAGE-CR-033: after merge, query from c2 does NOT inherit the covers edge

**Category:** resolution
**Policy:** tombstoning

**Precondition:** CR-032 has run, then c2 is added on Chain C (supersedes c1).

**Input:** `traverse(start_id=c2, edge_type=covers, direction=outbound, depth=2)`.

**Expected:** `covers` edge is NOT surfaced. No auto-inheritance across merges; successors may declare fresh `covers` edges if appropriate.

### TEST-SAGE-CR-034: historical query at (a8, b4) still surfaces the covers edge

**Category:** resolution
**Policy:** tombstoning

**Input:** `traverse(start_id=a8, edge_type=covers, direction=outbound, depth=2)`.

**Expected:** `covers` edge surfaces. The tombstone (`valid_until_version = a8`) marks suppression strictly DOWNSTREAM of a8 on the predecessor chain, not at a8 itself.

### TEST-SAGE-CR-035: time-travel query at pre-merge version — edge still surfaces

**Category:** resolution

**Input:** `traverse(start_id=a5, edge_type=covers, direction=outbound, depth=2)` (queried after the merge landed, but starting from a version that pre-dates the tombstone).

**Expected:** `covers` edge surfaces. Tombstoning is scoped by `valid_until_version` lineage; a5 is not downstream of a8.

### TEST-SAGE-CR-036: merged_from itself is policy none — no anchor fields permitted

**Category:** invariant

**Input:** `link(source_id=c1, target_id=a8, edge_type=merged_from, source_valid_from_version=c1)`.

**Expected:** 400 `EDGE_ANCHOR_POLICY_VIOLATION`. merged_from carries no anchor fields.

---

## Section 5 — resolution_path debug payload

### TEST-SAGE-CR-037: debug=false produces no resolution_path field

**Category:** contract

**Input:** `traverse(start_id=a5, edge_type=covers, depth=2)` (debug defaulted to false, explicit `debug: false`).

**Expected:** TraverseResponse has no `resolution_path` key (or the field is null). No per-event collection cost is incurred.

### TEST-SAGE-CR-038: debug=true records anchor_hit on surfaced edge

**Category:** debug

**Input:** `traverse(start_id=a5, edge_type=covers, depth=2, debug=true)`.

**Expected:** `resolution_path` contains an `anchor_hit` entry for the covers edge, with `anchor_field=source_valid_from_version`, `anchor_version=a3` (and a corresponding target-side check).

### TEST-SAGE-CR-039: debug=true records anchor_miss on suppressed edge

**Category:** debug

**Input:** `traverse(start_id=a2, edge_type=covers, depth=2, debug=true)`.

**Expected:** `resolution_path` contains an `anchor_miss` entry for the covers edge with `anchor_field=source_valid_from_version`, `anchor_version=a3` (the anchor the resolver checked and found absent from a2's lineage).

### TEST-SAGE-CR-040: debug=true records retracts_applied when a retraction suppresses an edge

**Category:** debug

**Precondition:** `retracts` edge anchored at a7 against the covers edge.

**Input:** `traverse(start_id=a8, edge_type=covers, depth=2, debug=true)`.

**Expected:** `resolution_path` contains a `retracts_applied` entry for the covers edge, with `retracted_edge_id` naming the retracts edge's id.

### TEST-SAGE-CR-041: debug=true records tombstone_applied when a merge tombstones an edge

**Category:** debug

**Precondition:** merged_from edge landed (c1 merged_from a8), covers edge has `valid_until_version = a8`. Chain C has c2 supersedes c1.

**Input:** `traverse(start_id=c2, edge_type=covers, depth=2, debug=true)`.

**Expected:** `resolution_path` contains a `tombstone_applied` entry for the covers edge with `tombstone_version=a8`.

### TEST-SAGE-CR-042: resolution_path preserves event order

**Category:** debug

**Input:** A single traverse that hits multiple edges producing a mix of anchor_hit, anchor_miss, and retracts_applied events.

**Expected:** Entries in `resolution_path` appear in the order the resolver processed them. Downstream consumers (App, MCP clients) can rely on chronological ordering for display.

---

## Section 6 — Registry coverage and TBD handling

### TEST-SAGE-CR-043: registry missing an entry for a used edge type — traverse fails fast

**Category:** configuration

**Precondition:** A vault registry that omits `covers` entirely. A `covers` edge exists (possibly from a legacy vault predating the registry).

**Input:** `traverse(start_id=a5, edge_type=covers, depth=2)`.

**Expected:** 400 or 500 with an unambiguous error: the edge type has no declared resolution_policy. The resolver does not silently fall back to `none` (which would mask config drift).

### TEST-SAGE-CR-044: migration script refuses to backfill TBD-policy edges

**Category:** migration

**Precondition:** Legacy vault has one edge with edge_type `authoritative_for` (policy TBD in the registry).

**Input:** Run `scripts/migrate_edge_anchors.py` (dry-run or write mode).

**Expected:** Migration halts with `TBD_POLICY_EDGE` and identifies the offending edge id(s). Operator must either promote the policy or delete the edge before re-running.

### TEST-SAGE-CR-045: registry with a TBD entry passes schema validation but blocks edge creation

**Category:** configuration

**Input:** Load a vault whose registry entry for `sync_target` has `resolution_policy: TBD`. Then attempt to create a `sync_target` edge.

**Expected:** Vault loads successfully. Edge creation returns 400 `TBD_POLICY_EDGE` (see CR-010). This test confirms the two checks are independent: registry schema accepts TBD; write-time validator rejects TBD edge creation.

---

## Section 7 — transitive_target (mirror of transitive_source)

These tests cover the fourth ADR-017 resolution policy: `transitive_target`.
Under this policy the target chain lives (advances via supersedes and
carries the target-side anchor); the source endpoint is frozen at
derivation and carries no anchor. This is the policy-level mirror of
`transitive_source`. No edge type in the built-in registry uses this
policy today; tests construct a custom `EdgeTypeRegistry` that maps an
otherwise-unused edge type to `transitive_target` so the resolver and
validator paths are exercised end-to-end.

### TEST-SAGE-CR-046: transitive_target edge with target anchor supplied — accepted

**Category:** valid
**Policy:** `transitive_target`

**Precondition:** Custom registry maps `authoritative_for` → `transitive_target`. Target chain `b1 ← b2`.

**Input:** `link(source_id=a3, target_id=b2, edge_type=authoritative_for, target_valid_from_version=b2)` (no `source_valid_from_version`).

**Expected:** 201. Created Edge carries `resolution_policy: transitive_target`, `source_valid_from_version: null`, `target_valid_from_version: b2`.

### TEST-SAGE-CR-047: transitive_target edge with missing target anchor — rejected

**Category:** invalid
**Policy:** `transitive_target`

**Input:** `link(source_id=a3, target_id=b2, edge_type=authoritative_for)` (no `target_valid_from_version`).

**Expected:** 400 `EDGE_ANCHOR_POLICY_VIOLATION`. `target_valid_from_version` is required for policy `transitive_target`. `offending_fields` includes `target_valid_from_version`.

### TEST-SAGE-CR-048: transitive_target edge with source anchor supplied — rejected

**Category:** invalid
**Policy:** `transitive_target`

**Input:** `link(source_id=a3, target_id=b2, edge_type=authoritative_for, source_valid_from_version=a3, target_valid_from_version=b2)`.

**Expected:** 400 `EDGE_ANCHOR_POLICY_VIOLATION`. Source anchor is frozen at derivation and must be null for policy `transitive_target`. `offending_fields` includes `source_valid_from_version`.

### TEST-SAGE-CR-049: transitive_target edge with target anchor outside target chain lineage — rejected

**Category:** invalid
**Policy:** `transitive_target`

**Precondition:** Target chain has b1, b2 (b2 supersedes b1). Another chain has c1.

**Input:** `link(source_id=a3, target_id=b2, edge_type=authoritative_for, target_valid_from_version=c1)` (target anchor points at a document not in b2's supersedes lineage).

**Expected:** 400 `EDGE_ANCHOR_POLICY_VIOLATION`.

### TEST-SAGE-CR-050: traverse with transitive_target — target anchor in lineage — surfaces (outbound from frozen source)

**Category:** resolution
**Policy:** `transitive_target`

**Precondition:** Target chain `b1 ← b2 ← b3`. Edge with `source_id=a3, target_id=b2, target_valid_from_version=b2, resolution_policy=transitive_target`.

**Input:** `traverse(start_id=a3, edge_type=authoritative_for, direction=outbound, depth=2)` and `traverse(start_id=b3, edge_type=authoritative_for, direction=inbound, depth=2)`.

**Expected:** Both traversals surface the edge. Outbound from the frozen source a3 seeds exactly [a3]; inbound from b3 seeds the b3 supersedes lineage, which includes b2.

### TEST-SAGE-CR-051: traverse with transitive_target — target anchor not in lineage — suppressed

**Category:** resolution
**Policy:** `transitive_target`

**Input:** Same edge as CR-050. `traverse(start_id=b1, edge_type=authoritative_for, direction=inbound, depth=2)` (b1 is upstream of the anchor b2).

**Expected:** Edge is NOT surfaced. Target anchor b2 is not in b1's lineage.

### TEST-SAGE-CR-052: traverse with transitive_target — source chain advances — frozen source is not seed-expanded

**Category:** resolution
**Policy:** `transitive_target`

**Precondition:** Source chain `a1 ← a2 ← a3`. Edge anchored at source=a3, target=b2, target anchor=b2, `resolution_policy=transitive_target`. (By policy, source anchor is null; source endpoint is frozen at a3.)

**Input:** `traverse(start_id=a2, edge_type=authoritative_for, direction=outbound, depth=2)` (a2 is an ancestor of a3 on the source chain).

**Expected:** Edge is NOT surfaced from a2. The source endpoint is frozen at a3; outbound seeds are [a2] (not a2's lineage), and no edge has source_id a2.

### TEST-SAGE-CR-053: retracts suppression applies to transitive_target edges

**Category:** resolution
**Policy:** `transitive_target` + retracts

**Precondition:** Same edge as CR-050. Retracts edge targeting it anchored at the source chain (a5 on source chain, where source chain now advances to a5). Note: retracting chain is the source chain here (the chain taking the governance action). Target chain progression is `b1 ← b2 ← b3`.

**Input:** `traverse(start_id=a5, edge_type=authoritative_for, direction=outbound, depth=2)`.

**Expected:** Edge is suppressed. The retracts primitive is one-sided per ADR-017 but the policy-level suppression rule is the same as for `transitive_source` / `transitive_both`: retracts anchored in the query start's supersedes lineage suppresses the target edge. Confirms `transitive_target` is a first-class suppressible policy.

---
