"""MCP tool tests for bulk_create_edge.

Exercises the boundary contract (vault_id and per-item shape validation,
registry membership check, round-trip of BulkLinkResponse through the
MCP serialize path), the per-item error-code parity with create_edge, the
natural-key idempotency on each item, the/dry-run
discipline, and the/response_mode rules.

Each group mirrors the conventions established by
test_sage_bulk_set_lifecycle.py and test_sage_bulk_update_metadata.py
(per CAS-ADR-029) and test_link_dry_run.py ( ).
"""

from __future__ import annotations

import pytest

from sage import mcp_server
from sage.adapters.stubs import StubContentStore
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.models.enums import EdgeType
from sage.models.schemas import BulkLinkResponse
from sage.services._dry_run import DRY_RUN_SENTINEL_EDGE_ID
from tests.sage._dry_run_helpers import assert_state_unchanged, state_snapshot
from tests.sage.test_lifecycle import _id, _make_doc

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch):
    """Snapshot mcp_server._vaults before each test and restore after."""
    saved = dict(mcp_server._vaults)
    mcp_server._vaults.clear()
    try:
        yield
    finally:
        mcp_server._vaults.clear()
        mcp_server._vaults.update(saved)


@pytest.fixture
async def seeded_mcp_vault(minimal_vault_config_dict, monkeypatch, empty_registry):
    """Boot a vault, seed four documents, publish on mcp_server._vaults.

    Four docs give every test enough endpoint material to assemble at
    least two distinct references edges in the same batch (each
    references edge has the natural-key triple (source, target, type),
    so distinct doc pairings produce distinct edges).
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id
    services = app.state.vault_registry[vault_id]
    mcp_server._vaults[vault_id] = services

    seeded_ids = [_id(f"doc_bulk_link_{n}") for n in range(4)]
    for doc_id in seeded_ids:
        await services.graph_store.insert_document(_make_doc(doc_id))

    yield vault_id, seeded_ids, services

    services.close_timing()
    await services.graph_store.close()


@pytest.fixture
async def seeded_seven_mcp_vault(minimal_vault_config_dict, monkeypatch, empty_registry):
    """Boot a vault and seed seven documents.

    Seven endpoints support the threshold-default tests: a 6-edge batch
    crosses the >5 default-threshold boundary using endpoint pairs
    (0,1), (0,2), (0,3), (0,4), (0,5), (0,6). The hub-and-spoke shape
    keeps each batch item's natural-key triple distinct.
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id
    services = app.state.vault_registry[vault_id]
    mcp_server._vaults[vault_id] = services

    seeded_ids = [_id(f"doc_bulk_link_seven_{n}") for n in range(7)]
    for doc_id in seeded_ids:
        await services.graph_store.insert_document(_make_doc(doc_id))

    yield vault_id, seeded_ids, services

    services.close_timing()
    await services.graph_store.close()


def _ref_item(source: str, target: str, **overrides) -> dict:
    """Construct a well-formed BulkLinkItem dict for a references edge.

    References edges have resolution_policy=transitive_both, so both
    anchor fields are required. The endpoint document ids double as
    their own chain-head anchors (each test doc is the head of a
    one-element supersedes lineage).
    """
    item = {
        "source_id": source,
        "target_id": target,
        "edge_type": "references",
        "source_valid_from_version": source,
        "target_valid_from_version": target,
    }
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# Group A — MCP round-trip & batch-level validation
# ---------------------------------------------------------------------------


async def test_a1_mcp_tool_round_trip_returns_dict_matching_response_model(seeded_mcp_vault):
    """A1 — The returned dict deserializes cleanly as BulkLinkResponse
    and the underlying edges are persisted in the graph store."""
    vault_id, ids, services = seeded_mcp_vault
    items = [_ref_item(ids[0], ids[1])]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items)

    assert isinstance(result, dict)
    assert "error" not in result, f"unexpected error envelope: {result!r}"
    response = BulkLinkResponse.model_validate(result)
    assert response.total == 1
    assert response.success_count == 1
    assert response.error_count == 0
    # Anti-coincidental-pass: confirm persistence rather than relying on
    # the response's self-report.
    persisted = await services.graph_store.get_edges_by_source(ids[0], "references")
    assert len(persisted) == 1
    assert persisted[0].target_id == ids[1]


async def test_a2_mcp_tool_invalid_vault_id_returns_error_envelope(empty_registry):
    """A2 — vault_id failing the VaultIdStr adapter surfaces the structured
    invalid_vault_id (400) envelope carrying the offending value."""
    result = await mcp_server.create_edges(vault_id="not a vault id!", items=[])
    assert isinstance(result, dict)
    assert "error" in result
    assert result["error"] == "invalid_vault_id"
    assert result["detail"]["vault_id"] == "not a vault id!"


async def test_a3_mcp_tool_unknown_vault_returns_error_envelope(empty_registry):
    """A3 — Valid-shape but unregistered vault_id surfaces unknown_vault."""
    result = await mcp_server.create_edges(vault_id="ghost", items=[])
    assert isinstance(result, dict)
    assert result.get("error") == "unknown_vault"


async def test_a4_mcp_tool_items_validation_rejects_batch_before_any_insert(
    seeded_mcp_vault,
):
    """A4 — A malformed item (missing edge_type) rejects the entire
    batch before any per-item work runs. Critically, the well-formed
    item at position 0 must NOT have committed."""
    vault_id, ids, services = seeded_mcp_vault
    bad_items = [
        _ref_item(ids[0], ids[1]),
        # Missing required field: edge_type
        {
            "source_id": ids[0],
            "target_id": ids[2],
            "source_valid_from_version": ids[0],
            "target_valid_from_version": ids[2],
        },
    ]

    result = await mcp_server.create_edges(vault_id=vault_id, items=bad_items)

    assert isinstance(result, dict)
    assert "error" in result, f"expected validation error envelope, got {result!r}"

    # Anti-coincidental-pass: the well-formed item must NOT have been
    # processed. If shape validation ran lazily inside the loop, item 0
    # would have committed before item 1 raised.
    persisted = await services.graph_store.get_edges_by_source(ids[0], "references")
    assert persisted == [], (
        "Up-front shape validation must reject the entire batch before "
        "any per-item work runs; no references edge should exist."
    )


async def test_a5_mcp_tool_empty_items_returns_zero_results(seeded_mcp_vault):
    """A5 — Empty items returns an empty results array with all counts zero
    and envelope success."""
    vault_id, _ids, _services = seeded_mcp_vault

    result = await mcp_server.create_edges(vault_id=vault_id, items=[])

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    assert result["results"] == []
    assert result["success_count"] == 0
    assert result["error_count"] == 0
    assert result["total"] == 0


async def test_a6_mcp_tool_invalid_response_mode_rejected_up_front(seeded_mcp_vault):
    """A6 — Invalid response_mode value rejects the batch before any
    per-item work. Otherwise valid items must NOT be persisted."""
    vault_id, ids, services = seeded_mcp_vault
    items = [_ref_item(ids[0], ids[1])]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items, response_mode="medium")

    assert "error" in result, f"expected validation error envelope, got {result!r}"
    persisted = await services.graph_store.get_edges_by_source(ids[0], "references")
    assert persisted == [], (
        "invalid response_mode must abort batch BEFORE per-item work; no "
        "edges should have been inserted."
    )


# ---------------------------------------------------------------------------
# Group B — Per-item error code parity with create_edge
#
# For each error code, the test seeds the documents and edge state
# needed to trigger it, includes the offending item alongside one valid
# item, and asserts:
# (1) the offending item's `error.error` code matches the named code,
# (2) the valid item committed (proves the batch did not short-circuit),
# (3) the offending item's edge did NOT commit.
# ---------------------------------------------------------------------------


async def test_b1_self_referential_edge_per_item(seeded_mcp_vault):
    """B1 — A self-referential edge (source == target) raises
    self_referential_edge per item; sibling valid item still commits."""
    vault_id, ids, services = seeded_mcp_vault
    items = [
        _ref_item(ids[0], ids[1]),  # valid
        _ref_item(ids[2], ids[2]),  # self-ref
    ]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items)

    assert "error" not in result
    assert result["success_count"] == 1
    assert result["error_count"] == 1
    assert result["results"][0]["status"] == "success"
    assert result["results"][1]["status"] == "error"
    assert result["results"][1]["error"]["error"] == "self_referential_edge"
    # Sibling valid item committed.
    persisted = await services.graph_store.get_edges_by_source(ids[0], "references")
    assert len(persisted) == 1


async def test_b2_edge_anchor_policy_violation_missing_transitive_both_per_item(
    seeded_mcp_vault,
):
    """B2 — A references edge (policy=transitive_both) missing the
    required anchor fields raises edge_anchor_policy_violation per item."""
    vault_id, ids, services = seeded_mcp_vault
    items = [
        _ref_item(ids[0], ids[1]),  # valid
        # Missing both anchor fields on a transitive_both edge.
        {
            "source_id": ids[2],
            "target_id": ids[3],
            "edge_type": "references",
        },
    ]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items)

    assert "error" not in result
    assert result["success_count"] == 1
    assert result["error_count"] == 1
    assert result["results"][1]["error"]["error"] == "edge_anchor_policy_violation"
    persisted = await services.graph_store.get_edges_by_source(ids[0], "references")
    assert len(persisted) == 1
    persisted_offender = await services.graph_store.get_edges_by_source(ids[2], "references")
    assert persisted_offender == []


async def test_b3_edge_anchor_policy_violation_forbidden_field_on_retracts_per_item(
    seeded_mcp_vault,
):
    """B3 — A retracts edge with target_id set (forbidden) raises
    edge_anchor_policy_violation per item."""
    vault_id, ids, services = seeded_mcp_vault
    # Seed a real edge for the retracts item to target.
    real_edge_response = await services.graph_ops_service._create_edge_strict(
        await _build_link_request(ids[0], ids[1])
    )
    real_edge_id = real_edge_response.edge.id

    items = [
        _ref_item(ids[0], ids[2]),  # valid sibling
        {
            "source_id": ids[3],
            "target_id": ids[1],  # forbidden on retracts edges
            "edge_type": "retracts",
            "retracted_edge_id": real_edge_id,
            "source_valid_from_version": ids[3],
        },
    ]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items)

    assert "error" not in result
    assert result["success_count"] == 1
    assert result["error_count"] == 1
    assert result["results"][1]["error"]["error"] == "edge_anchor_policy_violation"


async def test_b4_retract_target_not_edge_per_item(seeded_mcp_vault):
    """B4 — A retracts edge whose retracted_edge_id does not identify
    an existing edge raises retract_target_not_edge per item."""
    vault_id, ids, services = seeded_mcp_vault
    items = [
        _ref_item(ids[0], ids[1]),  # valid
        {
            "source_id": ids[2],
            "edge_type": "retracts",
            "retracted_edge_id": "11111111-1111-1111-1111-111111111111",
            "source_valid_from_version": ids[2],
        },
    ]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items)

    assert "error" not in result
    assert result["success_count"] == 1
    assert result["error_count"] == 1
    assert result["results"][1]["error"]["error"] == "retract_target_not_edge"


async def test_b5_document_not_found_source_per_item(seeded_mcp_vault):
    """B5 — A references edge whose source_id does not exist raises
    document_not_found per item."""
    vault_id, ids, services = seeded_mcp_vault
    ghost = _id("doc_ghost_b5")
    items = [
        _ref_item(ids[0], ids[1]),
        _ref_item(ghost, ids[2]),
    ]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items)

    assert "error" not in result
    assert result["success_count"] == 1
    assert result["results"][1]["status"] == "error"
    assert result["results"][1]["error"]["error"] == "document_not_found"


async def test_b6_document_not_found_target_per_item(seeded_mcp_vault):
    """B6 — A references edge whose target_id does not exist raises
    document_not_found per item."""
    vault_id, ids, services = seeded_mcp_vault
    ghost = _id("doc_ghost_b6")
    items = [
        _ref_item(ids[0], ids[1]),
        _ref_item(ids[2], ghost),
    ]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items)

    assert "error" not in result
    assert result["success_count"] == 1
    assert result["results"][1]["status"] == "error"
    assert result["results"][1]["error"]["error"] == "document_not_found"


# ---------------------------------------------------------------------------
# Group C — Idempotency & mixed batches
# ---------------------------------------------------------------------------


async def test_c1_per_item_idempotency_returns_created_false_on_natural_key_hit(
    seeded_mcp_vault,
):
    """C1 — Per-item natural-key idempotency: a request for an edge
    that already exists returns created=False with the existing edge's
    id; sibling request for a new edge returns created=True. Only one
    edge persisted for the collided natural key."""
    vault_id, ids, services = seeded_mcp_vault
    # Pre-seed: (ids[0], ids[1], references) already exists.
    pre_existing = await services.graph_ops_service._create_edge_strict(
        await _build_link_request(ids[0], ids[1], rationale="original-c1")
    )
    pre_id = pre_existing.edge.id

    items = [
        _ref_item(ids[0], ids[1]),  # collision
        _ref_item(ids[0], ids[2]),  # new
    ]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items)

    assert "error" not in result
    assert result["success_count"] == 2
    assert result["results"][0]["created"] is False
    assert result["results"][0]["edge"]["id"] == pre_id
    assert result["results"][1]["created"] is True
    # Anti-coincidental: only one edge for (ids[0], ids[1], references).
    edges_from_zero = await services.graph_store.get_edges_by_source(ids[0], "references")
    natural_key_hits = [e for e in edges_from_zero if e.target_id == ids[1]]
    assert len(natural_key_hits) == 1


async def test_c2_per_item_idempotency_returns_existing_rationale(seeded_mcp_vault):
    """C2 — On a natural-key hit, the per-item result carries the
    existing edge's rationale, not the request's rationale; the on-disk
    rationale is unchanged."""
    vault_id, ids, services = seeded_mcp_vault
    await services.graph_ops_service._create_edge_strict(
        await _build_link_request(ids[0], ids[1], rationale="r1")
    )

    items = [_ref_item(ids[0], ids[1], rationale="r2")]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items)

    assert "error" not in result
    entry = result["results"][0]
    assert entry["created"] is False
    assert entry["existing_rationale"] == "r1"
    edges = await services.graph_store.get_edges_by_source(ids[0], "references")
    assert len(edges) == 1
    assert edges[0].rationale == "r1", (
        "on-disk rationale must be preserved on natural-key hit; the "
        "request's rationale is discarded."
    )


async def test_c3_mixed_batch_partial_success_envelope_is_success(seeded_mcp_vault):
    """C3 — A mixed batch (valid, invalid, valid) reports envelope-level
    success with per-item statuses. Both valid items persist; the
    invalid one does not (no batch-wide rollback). Critically, the
    invalid item in the middle must not short-circuit the trailing
    valid item."""
    vault_id, ids, services = seeded_mcp_vault
    items = [
        _ref_item(ids[0], ids[1]),
        _ref_item(ids[2], ids[2]),  # self-ref → error
        _ref_item(ids[0], ids[3]),
    ]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items)

    assert "error" not in result, "envelope must be success per CAS-ADR-029"
    assert result["success_count"] == 2
    assert result["error_count"] == 1
    assert result["total"] == 3
    assert result["results"][0]["status"] == "success"
    assert result["results"][1]["status"] == "error"
    assert result["results"][2]["status"] == "success"

    # Anti-coincidental: both valid items persisted; invalid did not.
    edges_from_zero = await services.graph_store.get_edges_by_source(ids[0], "references")
    targets = sorted(e.target_id for e in edges_from_zero)
    assert targets == sorted([ids[1], ids[3]])
    edges_from_two = await services.graph_store.get_edges_by_source(ids[2], "references")
    assert edges_from_two == []


# ---------------------------------------------------------------------------
# Group D — Dry-run (/)
# ---------------------------------------------------------------------------


async def test_d1_dry_run_returns_sentinel_edge_id_per_item(seeded_mcp_vault, stub_content_store):
    """D1 — On a fresh natural key, dry-run returns the would-be edge
    with the nil-UUID sentinel id; envelope carries dry_run=True."""
    vault_id, ids, services = seeded_mcp_vault
    items = [_ref_item(ids[0], ids[1]), _ref_item(ids[0], ids[2])]

    result = await mcp_server.create_edges(
        vault_id=vault_id, items=items, dry_run=True, response_mode="full"
    )

    assert "error" not in result
    assert result["dry_run"] is True
    assert result["success_count"] == 2
    for entry in result["results"]:
        assert entry["status"] == "success"
        # Anti-coincidental-pass: assert literal equality with the
        # sentinel constant rather than just "looks like a UUID".
        assert entry["edge"]["id"] == DRY_RUN_SENTINEL_EDGE_ID


async def test_d2_dry_run_persists_no_edges(seeded_mcp_vault, stub_content_store):
    """D2 — Dry-run leaves the graph store fingerprint unchanged."""
    vault_id, ids, services = seeded_mcp_vault
    items = [_ref_item(ids[0], ids[1]), _ref_item(ids[0], ids[2])]

    before = await state_snapshot(services.graph_store)
    result = await mcp_server.create_edges(vault_id=vault_id, items=items, dry_run=True)
    after = await state_snapshot(services.graph_store)

    assert "error" not in result
    assert result["dry_run"] is True
    assert_state_unchanged(before, after)
    # Positive control: real-run actually inserts.
    await mcp_server.create_edges(vault_id=vault_id, items=items)
    edges_from_zero = await services.graph_store.get_edges_by_source(ids[0], "references")
    assert len(edges_from_zero) == 2


async def test_d3_dry_run_error_parity_per_item(seeded_mcp_vault):
    """D3 — A per-item error envelope from dry-run is byte-identical to
    the per-item error envelope from a real run for the same input
    (same-validator-paired contract)."""
    vault_id, ids, _services = seeded_mcp_vault
    items = [_ref_item(ids[0], ids[0])]  # self-ref

    real_result = await mcp_server.create_edges(vault_id=vault_id, items=items)
    dry_result = await mcp_server.create_edges(vault_id=vault_id, items=items, dry_run=True)

    real_err = real_result["results"][0]["error"]
    dry_err = dry_result["results"][0]["error"]
    assert real_err == dry_err, (
        f"dry-run error envelope must match real-run; real={real_err!r} dry={dry_err!r}"
    )
    assert dry_err["error"] == "self_referential_edge"


async def test_d4_dry_run_natural_key_collision_returns_existing_edge_with_created_false(
    seeded_mcp_vault,
):
    """D4 — On a natural-key collision, dry-run returns the existing
    edge id (NOT the sentinel) with created=False. Anti-coincidental:
    without the pre-check, dry-run would silently mint a
    sentinel and report created=True even though a real run would
    no-op."""
    vault_id, ids, services = seeded_mcp_vault
    real = await services.graph_ops_service._create_edge_strict(
        await _build_link_request(ids[0], ids[1])
    )
    real_id = real.edge.id

    result = await mcp_server.create_edges(
        vault_id=vault_id,
        items=[_ref_item(ids[0], ids[1])],
        dry_run=True,
        response_mode="full",
    )

    assert "error" not in result
    entry = result["results"][0]
    assert entry["status"] == "success"
    assert entry["created"] is False
    assert entry["edge"]["id"] == real_id
    assert entry["edge"]["id"] != DRY_RUN_SENTINEL_EDGE_ID
    assert result["dry_run"] is True


async def test_d5_dry_run_envelope_carries_dry_run_true(seeded_mcp_vault):
    """D5 — The envelope echoes dry_run; pair-test against real-run on
    the same input to catch an always-True or always-False bug."""
    vault_id, ids, _services = seeded_mcp_vault
    items = [_ref_item(ids[0], ids[1])]

    dry_result = await mcp_server.create_edges(vault_id=vault_id, items=items, dry_run=True)
    # Distinct natural key on the real-run so we don't conflate with C4.
    real_items = [_ref_item(ids[0], ids[2])]
    real_result = await mcp_server.create_edges(vault_id=vault_id, items=real_items)

    assert dry_result["dry_run"] is True
    assert real_result["dry_run"] is False


# ---------------------------------------------------------------------------
# Group E — response_mode (/)
# ---------------------------------------------------------------------------


async def test_e1_response_mode_light_strips_edge_body_on_success(seeded_mcp_vault):
    """E1 — Explicit response_mode='light' drops the per-item edge body
    from success entries. Pair against E2 confirms this is a mode-
    specific strip, not an always-strip."""
    vault_id, ids, _services = seeded_mcp_vault
    items = [_ref_item(ids[0], ids[1]), _ref_item(ids[0], ids[2])]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items, response_mode="light")

    assert "error" not in result
    assert result["success_count"] == 2
    for entry in result["results"]:
        assert entry["status"] == "success"
        # MCP _serialize uses exclude_none=True, so a None `edge` is
        # stripped from the wire payload entirely.
        assert "edge" not in entry, (
            f"light mode must strip the per-item `edge` field; got {entry!r}"
        )


async def test_e2_response_mode_full_preserves_edge_body(seeded_mcp_vault):
    """E2 — Explicit response_mode='full' preserves the per-item edge body."""
    vault_id, ids, _services = seeded_mcp_vault
    items = [_ref_item(ids[0], ids[1]), _ref_item(ids[0], ids[2])]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items, response_mode="full")

    assert "error" not in result
    for entry in result["results"]:
        assert entry["status"] == "success"
        # Anti-coincidental-pass: assert the inner shape, not just truthiness.
        assert entry["edge"]["edge_type"] == "references"
        assert entry["edge"]["id"]  # non-empty UUID string


async def test_e3_response_mode_default_above_threshold_returns_light(
    seeded_seven_mcp_vault,
):
    """E3 — Default mode with a 6-item batch (one above the >5
    threshold) returns light. Critical anti-coincidental-pass:
    `response_mode` is NOT passed; this exercises the default-
    resolution branch."""
    vault_id, ids, _services = seeded_seven_mcp_vault
    items = [_ref_item(ids[0], ids[n]) for n in range(1, 7)]  # 6 items

    result = await mcp_server.create_edges(vault_id=vault_id, items=items)

    assert "error" not in result
    assert result["success_count"] == 6
    for entry in result["results"]:
        assert "edge" not in entry, f"default above threshold (6>5) must be light; got {entry!r}"


async def test_e4_response_mode_default_at_or_below_threshold_returns_full(
    seeded_mcp_vault,
):
    """E4 — Default mode with a 3-item batch (at-or-below threshold)
    returns full. If the threshold comparison is `>=` instead of `>`,
    behavior at len==5 silently flips."""
    vault_id, ids, _services = seeded_mcp_vault
    items = [
        _ref_item(ids[0], ids[1]),
        _ref_item(ids[0], ids[2]),
        _ref_item(ids[0], ids[3]),
    ]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items)

    assert "error" not in result
    assert result["success_count"] == 3
    for entry in result["results"]:
        assert entry["edge"]["edge_type"] == "references"


async def test_e5_response_mode_error_envelope_intact_in_light_mode(seeded_mcp_vault):
    """E5 — An error item in light mode keeps the full error envelope.
    Round-trip against the same shape in full mode; envelopes must be
    byte-identical so light does not strip actionable error structure."""
    vault_id, ids, _services = seeded_mcp_vault
    light_items = [_ref_item(ids[0], ids[0])]  # self-ref
    full_items = [_ref_item(ids[1], ids[1])]  # different self-ref so no cross-pollution

    light_result = await mcp_server.create_edges(
        vault_id=vault_id, items=light_items, response_mode="light"
    )
    full_result = await mcp_server.create_edges(
        vault_id=vault_id, items=full_items, response_mode="full"
    )

    light_err = light_result["results"][0]["error"]
    full_err = full_result["results"][0]["error"]
    assert light_err["error"] == "self_referential_edge"
    assert full_err["error"] == "self_referential_edge"
    # The detail dict is structured under self_referential_edge; both
    # modes must preserve it.
    assert "detail" in light_err
    assert "detail" in full_err


async def test_e6_response_mode_mixed_batch_in_light_mode(seeded_mcp_vault):
    """E6 — Mixed-result batch in light mode: success items have no
    `edge`, error items have full `error`. Catches a per-item-loop bug
    where mode is applied to only the first item or the wrong branch."""
    vault_id, ids, _services = seeded_mcp_vault
    items = [
        _ref_item(ids[0], ids[1]),  # success
        _ref_item(ids[2], ids[2]),  # error (self-ref)
        _ref_item(ids[0], ids[3]),  # success
    ]

    result = await mcp_server.create_edges(vault_id=vault_id, items=items, response_mode="light")

    assert result["success_count"] == 2
    assert result["error_count"] == 1
    assert result["total"] == 3
    assert result["results"][0]["status"] == "success"
    assert "edge" not in result["results"][0]
    assert result["results"][1]["status"] == "error"
    assert result["results"][1]["error"]["error"] == "self_referential_edge"
    assert result["results"][2]["status"] == "success"
    assert "edge" not in result["results"][2]


# ---------------------------------------------------------------------------
# Internal test helpers
# ---------------------------------------------------------------------------


async def _build_link_request(source: str, target: str, *, rationale: str | None = None):
    """Lazy import so this module loads cleanly even before the new
    BulkLink* models exist (Phase-3 failing-tests safety)."""
    from sage.models.schemas import LinkRequest

    return LinkRequest(
        source_id=source,
        target_id=target,
        edge_type=EdgeType.REFERENCES,
        source_valid_from_version=source,
        target_valid_from_version=target,
        rationale=rationale,
    )
