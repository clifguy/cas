"""`unlink` dry-run.

unlink takes no request body (path param edge_id), so ``dry_run`` is a
separate method parameter on the service (and a query parameter on the
REST router).

Three test categories:

A. Happy-path dry-run — response carries `deleted=False`, the
   `preview_edge`, and `dry_run=True`; the edge is still present in
   storage after the call.
B. Same-validator paired — EdgeNotFoundError raised by both paths with
   identical envelope.
C. Side-effect-specific — real-run deletes (positive control); dry-run
   leaves storage byte-identical.
"""

from __future__ import annotations

import pytest

from sage.api.errors import EdgeNotFoundError
from sage.models.enums import EdgeType
from sage.models.schemas import LinkRequest, UnlinkResponse
from tests.sage._dry_run_helpers import assert_state_unchanged, state_snapshot
from tests.sage.test_lifecycle import _id, _make_doc


async def _make_edge(graph_store, graph_ops_service) -> str:
    """Insert two docs and create a references edge; return edge_id."""
    await graph_store.insert_document(_make_doc(_id("doc_a")))
    await graph_store.insert_document(_make_doc(_id("doc_b")))
    response = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
        )
    )
    return response.edge.id


# ---------------------------------------------------------------------------
# (A) Happy-path dry-run
# ---------------------------------------------------------------------------


async def test_dry_run_returns_preview_without_deleting(
    graph_store, graph_ops_service, stub_content_store
):
    """Dry-run unlink returns the would-be-deleted edge as preview_edge;
    the edge remains in storage after the call."""
    edge_id = await _make_edge(graph_store, graph_ops_service)

    before = await state_snapshot(graph_store, stub_content_store)

    response = await graph_ops_service.unlink(edge_id, dry_run=True)

    after = await state_snapshot(graph_store, stub_content_store)

    assert isinstance(response, UnlinkResponse)
    assert response.dry_run is True
    assert response.deleted is False
    assert response.edge_id == edge_id
    assert response.preview_edge is not None
    assert response.preview_edge.id == edge_id
    assert_state_unchanged(before, after)


async def test_real_run_returns_deleted_true_and_no_preview(graph_store, graph_ops_service):
    """Positive control: real-run returns deleted=True, preview_edge=None,
    dry_run=False; the edge is no longer in storage."""
    edge_id = await _make_edge(graph_store, graph_ops_service)

    response = await graph_ops_service.unlink(edge_id)
    assert response.dry_run is False
    assert response.deleted is True
    assert response.edge_id == edge_id
    assert response.preview_edge is None
    assert await graph_store.get_edge(edge_id) is None


# ---------------------------------------------------------------------------
# (B) Same-validator paired
# ---------------------------------------------------------------------------


async def test_edge_not_found_envelope_identical_under_dry_run(graph_ops_service):
    """Both paths raise EdgeNotFoundError with identical envelope."""
    import uuid

    missing = str(uuid.uuid4())

    with pytest.raises(EdgeNotFoundError) as real_info:
        await graph_ops_service.unlink(missing)
    with pytest.raises(EdgeNotFoundError) as dry_info:
        await graph_ops_service.unlink(missing, dry_run=True)

    assert real_info.value.code == dry_info.value.code
    assert real_info.value.detail == dry_info.value.detail


# ---------------------------------------------------------------------------
# (C) Side-effect-specific
# ---------------------------------------------------------------------------


async def test_dry_run_leaves_edge_in_storage(graph_store, graph_ops_service, stub_content_store):
    """Anti-coincidental: dry-run unlink must not delete. The edge is
    still queryable from storage after the call. Real-run does delete
    (paired positive control)."""
    edge_id = await _make_edge(graph_store, graph_ops_service)

    await graph_ops_service.unlink(edge_id, dry_run=True)
    assert await graph_store.get_edge(edge_id) is not None

    await graph_ops_service.unlink(edge_id)
    assert await graph_store.get_edge(edge_id) is None
