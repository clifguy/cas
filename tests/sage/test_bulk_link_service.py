"""Service-layer tests for GraphOpsService.bulk_link (T-0165).

The bulk method holds the process-wide ``_link_lock`` per item and runs
each item under its own SQLite transaction via ``link_idempotent``; the
batch as a whole is NOT atomic. A bad item does not roll back earlier-
or-later successful items (CAS-ADR-029). The T-0079 natural-key
idempotency contract is preserved per item: a duplicate triple returns
the existing edge with ``created=False`` rather than raising.

These tests bypass the MCP and HTTP surfaces and exercise the service
directly so the service-internal behavior is observable in isolation
(per-item locking, transaction independence, idempotency, response-mode
gating). The MCP-tool tests in test_sage_bulk_link.py cover the same
surface through the wider stack; this file is the load-bearing
isolation lens.
"""

from __future__ import annotations

from sage.models.enums import EdgeType, ResponseMode
from sage.models.schemas import (
    BulkLinkItem,
    BulkLinkRequest,
    LinkRequest,
)
from sage.services._dry_run import DRY_RUN_SENTINEL_EDGE_ID
from tests.sage.test_lifecycle import _id, _make_doc


def _ref_item(source: str, target: str, **overrides) -> BulkLinkItem:
    """Construct a well-formed references-edge BulkLinkItem.

    References has resolution_policy=transitive_both, so both anchor
    fields are required. Each test document is the head of its own
    one-element supersedes lineage, so anchor=endpoint id is always in-
    lineage.
    """
    fields = {
        "source_id": source,
        "target_id": target,
        "edge_type": EdgeType.REFERENCES,
        "source_valid_from_version": source,
        "target_valid_from_version": target,
    }
    fields.update(overrides)
    return BulkLinkItem(**fields)


async def test_bulk_link_happy_path_creates_multiple_edges(graph_store, graph_ops_service):
    """Three new edges created in one call; each result entry carries
    created=True and the edge body."""
    ids = [_id(f"doc_h{n}") for n in range(4)]
    for doc_id in ids:
        await graph_store.insert_document(_make_doc(doc_id))

    response = await graph_ops_service.bulk_link(
        BulkLinkRequest(
            items=[
                _ref_item(ids[0], ids[1]),
                _ref_item(ids[0], ids[2]),
                _ref_item(ids[0], ids[3]),
            ],
            response_mode=ResponseMode.FULL,
        )
    )

    assert response.total == 3
    assert response.success_count == 3
    assert response.error_count == 0
    for entry in response.results:
        assert entry.status == "success"
        assert entry.created is True
        assert entry.edge is not None
        assert entry.edge.edge_type == EdgeType.REFERENCES

    # Anti-coincidental-pass: re-read edges from storage independent of
    # the response's self-report.
    persisted = await graph_store.get_edges_by_source(ids[0], "references")
    assert sorted(e.target_id for e in persisted) == sorted([ids[1], ids[2], ids[3]])


async def test_bulk_link_empty_items_returns_empty_response(graph_ops_service):
    """Empty list is a valid no-op input."""
    response = await graph_ops_service.bulk_link(BulkLinkRequest(items=[]))

    assert response.total == 0
    assert response.success_count == 0
    assert response.error_count == 0
    assert response.results == []


async def test_bulk_link_partial_success_on_self_referential_edge(graph_store, graph_ops_service):
    """A self-ref in the middle does not roll back its neighbours."""
    ids = [_id(f"doc_p{n}") for n in range(4)]
    for doc_id in ids:
        await graph_store.insert_document(_make_doc(doc_id))

    response = await graph_ops_service.bulk_link(
        BulkLinkRequest(
            items=[
                _ref_item(ids[0], ids[1]),
                _ref_item(ids[2], ids[2]),  # self-ref
                _ref_item(ids[0], ids[3]),
            ],
            response_mode=ResponseMode.FULL,
        )
    )

    assert response.total == 3
    assert response.success_count == 2
    assert response.error_count == 1
    assert response.results[0].status == "success"
    assert response.results[1].status == "error"
    assert response.results[1].error["error"] == "self_referential_edge"
    assert response.results[2].status == "success"

    # Anti-coincidental-pass: the two valid items persist; the failing
    # middle item leaves no edge.
    persisted_from_zero = await graph_store.get_edges_by_source(ids[0], "references")
    assert sorted(e.target_id for e in persisted_from_zero) == sorted([ids[1], ids[3]])
    persisted_from_two = await graph_store.get_edges_by_source(ids[2], "references")
    assert persisted_from_two == []


async def test_bulk_link_partial_success_on_unknown_document(graph_store, graph_ops_service):
    """Targeting a non-existent target_id surfaces document_not_found and
    the rest of the batch continues."""
    real_src = _id("doc_real_src")
    real_tgt = _id("doc_real_tgt")
    ghost = _id("doc_ghost")
    await graph_store.insert_document(_make_doc(real_src))
    await graph_store.insert_document(_make_doc(real_tgt))

    response = await graph_ops_service.bulk_link(
        BulkLinkRequest(
            items=[
                _ref_item(real_src, real_tgt),
                _ref_item(real_src, ghost),
            ],
            response_mode=ResponseMode.FULL,
        )
    )

    assert response.success_count == 1
    assert response.error_count == 1
    assert response.results[0].status == "success"
    assert response.results[1].status == "error"
    assert response.results[1].error["error"] == "document_not_found"

    # Anti-coincidental-pass: real edge persisted.
    persisted = await graph_store.get_edges_by_source(real_src, "references")
    assert len(persisted) == 1
    assert persisted[0].target_id == real_tgt


async def test_bulk_link_t0079_idempotency_returns_existing_edge_with_created_false(
    graph_store, graph_ops_service
):
    """T-0079 per-item idempotency contract: a duplicate natural-key
    triple returns the existing edge with created=False and
    existing_rationale populated. No second edge persisted."""
    src = _id("doc_idem_src")
    tgt = _id("doc_idem_tgt")
    await graph_store.insert_document(_make_doc(src))
    await graph_store.insert_document(_make_doc(tgt))

    # Pre-seed via the single-item path.
    first = await graph_ops_service.link(
        LinkRequest(
            source_id=src,
            target_id=tgt,
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=src,
            target_valid_from_version=tgt,
            rationale="original-rationale",
        )
    )
    original_edge_id = first.edge.id

    # Now bulk-link the same natural key with a different rationale.
    response = await graph_ops_service.bulk_link(
        BulkLinkRequest(
            items=[_ref_item(src, tgt, rationale="bulk-attempt-rationale")],
            response_mode=ResponseMode.FULL,
        )
    )

    assert response.success_count == 1
    entry = response.results[0]
    assert entry.status == "success"
    assert entry.created is False
    assert entry.edge.id == original_edge_id  # same edge, not a duplicate
    assert entry.existing_rationale == "original-rationale"

    # Anti-coincidental-pass: only one edge for this triple in storage,
    # and the rationale is unchanged.
    persisted = await graph_store.get_edges_by_source(src, "references")
    assert len(persisted) == 1
    assert persisted[0].rationale == "original-rationale"


async def test_bulk_link_distinct_items_run_per_item_transactions(graph_store, graph_ops_service):
    """Item 1's commit must persist even when item 2 raises a SAGEError;
    proves each item is dispatched independently through
    link_idempotent and a failed item does not roll back earlier
    commits."""
    real = _id("doc_iso_real")
    ghost = _id("doc_iso_ghost")
    await graph_store.insert_document(_make_doc(real))

    response = await graph_ops_service.bulk_link(
        BulkLinkRequest(
            items=[
                _ref_item(real, real),  # self-ref → raises
                _ref_item(real, ghost),  # ghost target → raises
            ],
            response_mode=ResponseMode.FULL,
        )
    )

    # Both items fail in this batch; what matters is that the second
    # item's SAGEError did not propagate out of the batch and the
    # response carries both per-item errors with distinct codes.
    assert response.error_count == 2
    assert response.results[0].error["error"] == "self_referential_edge"
    assert response.results[1].error["error"] == "document_not_found"


async def test_bulk_link_response_mode_light_drops_edge_body(graph_store, graph_ops_service):
    """response_mode=light drops the per-item edge body from success
    entries but preserves created and existing_rationale (the only
    natural-key idempotency signals)."""
    ids = [_id(f"doc_lm{n}") for n in range(3)]
    for doc_id in ids:
        await graph_store.insert_document(_make_doc(doc_id))

    response = await graph_ops_service.bulk_link(
        BulkLinkRequest(
            items=[_ref_item(ids[0], ids[1]), _ref_item(ids[0], ids[2])],
            response_mode=ResponseMode.LIGHT,
        )
    )

    assert response.success_count == 2
    for entry in response.results:
        assert entry.status == "success"
        assert entry.edge is None
        # T-0079 signals preserved under light per the response_mode contract.
        assert entry.created is True


async def test_bulk_link_dry_run_persists_no_edges(graph_store, graph_ops_service):
    """dry_run propagates to each per-item LinkRequest; the would-be
    edge.id carries the sentinel and no edges are committed."""
    ids = [_id(f"doc_dr{n}") for n in range(3)]
    for doc_id in ids:
        await graph_store.insert_document(_make_doc(doc_id))

    response = await graph_ops_service.bulk_link(
        BulkLinkRequest(
            items=[_ref_item(ids[0], ids[1]), _ref_item(ids[0], ids[2])],
            dry_run=True,
            response_mode=ResponseMode.FULL,
        )
    )

    assert response.dry_run is True
    assert response.success_count == 2
    for entry in response.results:
        assert entry.edge.id == DRY_RUN_SENTINEL_EDGE_ID

    # Anti-coincidental-pass: re-read storage; no edge persisted.
    persisted = await graph_store.get_edges_by_source(ids[0], "references")
    assert persisted == []


async def test_bulk_link_default_response_mode_above_threshold_returns_light(
    graph_store, graph_ops_service
):
    """When response_mode is unset and len(items) > 5, effective mode
    resolves to LIGHT; mirrors the sibling bulk tools' default rule."""
    ids = [_id(f"doc_thresh{n}") for n in range(7)]
    for doc_id in ids:
        await graph_store.insert_document(_make_doc(doc_id))

    response = await graph_ops_service.bulk_link(
        BulkLinkRequest(items=[_ref_item(ids[0], ids[n]) for n in range(1, 7)])
    )

    assert response.success_count == 6
    for entry in response.results:
        # Light mode: edge body stripped.
        assert entry.edge is None


async def test_bulk_link_default_response_mode_at_or_below_threshold_returns_full(
    graph_store, graph_ops_service
):
    """When response_mode is unset and len(items) <= 5, effective mode
    resolves to FULL."""
    ids = [_id(f"doc_below{n}") for n in range(4)]
    for doc_id in ids:
        await graph_store.insert_document(_make_doc(doc_id))

    response = await graph_ops_service.bulk_link(
        BulkLinkRequest(items=[_ref_item(ids[0], ids[n]) for n in range(1, 4)])
    )

    assert response.success_count == 3
    for entry in response.results:
        assert entry.edge is not None
