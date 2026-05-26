"""`sage_link` dry-run, including the natural-key pre-check.

Three test categories per the plan:

A. Happy-path dry-run — response carries the would-be edge with the
   nil-UUID sentinel id and `dry_run=True`; state fingerprint is
   unchanged. Includes the natural-key collision case: dry-run on a
   pair that already has an edge returns `created=False` with the
   existing edge id (the pre-check lifts the storage-layer uniqueness
   gap to the application layer so dry-run can surface it).
B. Same-validator paired — identical inputs that hit a known error;
   real-run and dry-run produce the same error envelope.
C. Side-effect-specific — no edge inserted after dry-run.
"""

from __future__ import annotations

import pytest

from sage.api.errors import (
    DocumentNotFoundError,
    SelfReferentialEdgeError,
)
from sage.models.enums import EdgeType
from sage.models.schemas import LinkRequest, LinkResponse
from sage.services._dry_run import DRY_RUN_SENTINEL_EDGE_ID
from tests.sage._dry_run_helpers import assert_state_unchanged, state_snapshot
from tests.sage.test_lifecycle import _id, _make_doc

# ---------------------------------------------------------------------------
# (A) Happy-path dry-run
# ---------------------------------------------------------------------------


async def test_dry_run_returns_would_be_edge_with_sentinel_id(
    graph_store, graph_ops_service, stub_content_store
):
    """Dry-run on a fresh (source, target, edge_type) returns a
    LinkResponse with the sentinel id and `created=True`; state is
    byte-identical to pre-call."""
    await graph_store.insert_document(_make_doc(_id("doc_a")))
    await graph_store.insert_document(_make_doc(_id("doc_b")))

    before = await state_snapshot(graph_store, stub_content_store)

    response = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            rationale="dry-run preview",
            dry_run=True,
        )
    )

    after = await state_snapshot(graph_store, stub_content_store)

    assert isinstance(response, LinkResponse)
    assert response.dry_run is True
    assert response.created is True
    assert response.existing_rationale is None
    assert response.edge.id == DRY_RUN_SENTINEL_EDGE_ID
    assert response.edge.source_id == _id("doc_a")
    assert response.edge.target_id == _id("doc_b")
    assert response.edge.edge_type == EdgeType.REFERENCES
    assert response.edge.rationale == "dry-run preview"
    assert_state_unchanged(before, after)


async def test_dry_run_on_natural_key_collision_returns_existing_edge(
    graph_store, graph_ops_service, stub_content_store
):
    """pre-check (lifted from storage to application layer for
    ): dry-run via `link_idempotent` on a (source, target,
    edge_type) that already has an edge returns the existing edge with
    `created=False` — same shape as the real-run no-op path.

    Anti-coincidental: without the pre-check, the dry-run would
    silently report `created=True` for what would actually be a
    real-run no-op (because the storage uniqueness constraint never
    fires when no insert happens)."""
    await graph_store.insert_document(_make_doc(_id("doc_a")))
    await graph_store.insert_document(_make_doc(_id("doc_b")))

    first = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            rationale="original",
        )
    )

    before = await state_snapshot(graph_store, stub_content_store)

    dry_edge, dry_created = await graph_ops_service.link_idempotent(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            rationale="would be ignored on collision",
            dry_run=True,
        )
    )

    after = await state_snapshot(graph_store, stub_content_store)

    assert dry_created is False
    assert dry_edge.id == first.edge.id  # NOT the sentinel
    assert dry_edge.id != DRY_RUN_SENTINEL_EDGE_ID
    assert dry_edge.rationale == "original"
    assert_state_unchanged(before, after)


async def test_real_run_link_returns_link_response_with_dry_run_false(
    graph_store, graph_ops_service
):
    """Positive control: real-run returns LinkResponse with dry_run=False
    and a real UUID id."""
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
    assert isinstance(response, LinkResponse)
    assert response.dry_run is False
    assert response.created is True
    assert response.edge.id != DRY_RUN_SENTINEL_EDGE_ID
    # The persisted edge is queryable.
    edges = await graph_store.get_edges_by_source(_id("doc_a"), "references")
    assert len(edges) == 1
    assert edges[0].id == response.edge.id


# ---------------------------------------------------------------------------
# (B) Same-validator paired
# ---------------------------------------------------------------------------


async def test_source_not_found_envelope_identical_under_dry_run(graph_store, graph_ops_service):
    """Both paths raise DocumentNotFoundError when source is missing."""
    await graph_store.insert_document(_make_doc(_id("doc_b")))

    with pytest.raises(DocumentNotFoundError) as real_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("doc_missing_src"),
                target_id=_id("doc_b"),
                edge_type=EdgeType.REFERENCES,
                source_valid_from_version=_id("doc_missing_src"),
                target_valid_from_version=_id("doc_b"),
            )
        )
    with pytest.raises(DocumentNotFoundError) as dry_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("doc_missing_src"),
                target_id=_id("doc_b"),
                edge_type=EdgeType.REFERENCES,
                source_valid_from_version=_id("doc_missing_src"),
                target_valid_from_version=_id("doc_b"),
                dry_run=True,
            )
        )

    assert real_info.value.code == dry_info.value.code
    assert real_info.value.detail == dry_info.value.detail


async def test_target_not_found_envelope_identical_under_dry_run(graph_store, graph_ops_service):
    """Both paths raise DocumentNotFoundError when target is missing."""
    await graph_store.insert_document(_make_doc(_id("doc_a")))

    with pytest.raises(DocumentNotFoundError) as real_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("doc_a"),
                target_id=_id("doc_missing_tgt"),
                edge_type=EdgeType.REFERENCES,
                source_valid_from_version=_id("doc_a"),
                target_valid_from_version=_id("doc_missing_tgt"),
            )
        )
    with pytest.raises(DocumentNotFoundError) as dry_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("doc_a"),
                target_id=_id("doc_missing_tgt"),
                edge_type=EdgeType.REFERENCES,
                source_valid_from_version=_id("doc_a"),
                target_valid_from_version=_id("doc_missing_tgt"),
                dry_run=True,
            )
        )

    assert real_info.value.code == dry_info.value.code
    assert real_info.value.detail == dry_info.value.detail


async def test_self_referential_envelope_identical_under_dry_run(graph_store, graph_ops_service):
    """Both paths raise SelfReferentialEdgeError when source==target."""
    await graph_store.insert_document(_make_doc(_id("doc_a")))

    with pytest.raises(SelfReferentialEdgeError) as real_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("doc_a"),
                target_id=_id("doc_a"),
                edge_type=EdgeType.REFERENCES,
                source_valid_from_version=_id("doc_a"),
                target_valid_from_version=_id("doc_a"),
            )
        )
    with pytest.raises(SelfReferentialEdgeError) as dry_info:
        await graph_ops_service.link(
            LinkRequest(
                source_id=_id("doc_a"),
                target_id=_id("doc_a"),
                edge_type=EdgeType.REFERENCES,
                source_valid_from_version=_id("doc_a"),
                target_valid_from_version=_id("doc_a"),
                dry_run=True,
            )
        )

    assert real_info.value.code == dry_info.value.code
    assert real_info.value.detail == dry_info.value.detail


# ---------------------------------------------------------------------------
# (C) Side-effect-specific
# ---------------------------------------------------------------------------


async def test_dry_run_persists_no_edge(graph_store, graph_ops_service, stub_content_store):
    """Anti-coincidental: dry-run must NOT call insert_edge. Positive
    control: real-run does."""
    await graph_store.insert_document(_make_doc(_id("doc_a")))
    await graph_store.insert_document(_make_doc(_id("doc_b")))

    before = await state_snapshot(graph_store, stub_content_store)

    await graph_ops_service.link(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
            dry_run=True,
        )
    )
    after_dry = await state_snapshot(graph_store, stub_content_store)
    assert_state_unchanged(before, after_dry)
    edges = await graph_store.get_edges_by_source(_id("doc_a"), "references")
    assert edges == []

    # Positive control: real-run DOES insert.
    await graph_ops_service.link(
        LinkRequest(
            source_id=_id("doc_a"),
            target_id=_id("doc_b"),
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=_id("doc_a"),
            target_valid_from_version=_id("doc_b"),
        )
    )
    edges = await graph_store.get_edges_by_source(_id("doc_a"), "references")
    assert len(edges) == 1
