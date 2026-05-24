"""T-0152: `set_lifecycle` dry-run (including the supersede path).

Three test categories per the plan:

A. Happy-path dry-run — response carries the would-be document and
   `dry_run=True`; for supersede, the would-be edge surfaces in
   `created_edge` with sentinel id `<dry-run>`. State fingerprint is
   unchanged.
B. Same-validator paired — identical inputs that hit a known error;
   real-run and dry-run produce the same error envelope.
C. Side-effect-specific — `update_chunk_metadata` not called;
   `updated_at` and `lifecycle_status` unchanged on dry-run. For
   supersede dry-run: no `supersedes` edge persisted.
"""

from __future__ import annotations

import pytest

from sage.api.errors import (
    DocumentNotFoundError,
    InvalidActionError,
    InvalidLifecycleTransitionError,
    MissingFieldError,
)
from sage.models.enums import EdgeType
from sage.models.schemas import SetLifecycleRequest
from sage.services._dry_run import DRY_RUN_SENTINEL_EDGE_ID
from tests.sage._dry_run_helpers import assert_state_unchanged, state_snapshot
from tests.sage.test_lifecycle import _id, _make_doc

# ---------------------------------------------------------------------------
# (A) Happy-path dry-run
# ---------------------------------------------------------------------------


async def test_dry_run_returns_post_transition_document_without_writing(
    graph_store, lifecycle_service, stub_content_store
):
    """Non-supersede dry-run: response.document carries the would-be
    lifecycle_status; storage and chunk pushdown are unchanged."""
    doc = _make_doc(_id("doc_a"))
    await graph_store.insert_document(doc)

    before = await state_snapshot(graph_store, stub_content_store)

    response = await lifecycle_service.set_lifecycle(
        _id("doc_a"),
        SetLifecycleRequest(action="archive", dry_run=True),
    )

    after = await state_snapshot(graph_store, stub_content_store)

    assert response.dry_run is True
    assert response.document.lifecycle_status == "archived"
    assert response.created_edge is None  # non-supersede actions never create edges
    assert_state_unchanged(before, after)


async def test_supersede_dry_run_returns_would_be_edge_with_sentinel_id(
    graph_store, lifecycle_service, stub_content_store
):
    """Supersede dry-run: response.created_edge carries the would-be
    Edge with sentinel id `<dry-run>` and the right source/target;
    no edge is actually persisted."""
    doc_old = _make_doc(_id("doc_supersede_old"))
    doc_new = _make_doc(_id("doc_supersede_new"))
    await graph_store.insert_document(doc_old)
    await graph_store.insert_document(doc_new)

    before = await state_snapshot(graph_store, stub_content_store)

    response = await lifecycle_service.set_lifecycle(
        _id("doc_supersede_old"),
        SetLifecycleRequest(
            action="supersede",
            new_version_id=_id("doc_supersede_new"),
            dry_run=True,
        ),
    )

    after = await state_snapshot(graph_store, stub_content_store)

    assert response.dry_run is True
    assert response.document.lifecycle_status == "archived"  # would-be state
    assert response.created_edge is not None
    assert response.created_edge.id == DRY_RUN_SENTINEL_EDGE_ID
    assert response.created_edge.source_id == _id("doc_supersede_new")
    assert response.created_edge.target_id == _id("doc_supersede_old")
    assert response.created_edge.edge_type == EdgeType.SUPERSEDES
    # State byte-identical: no edges, no lifecycle change.
    assert_state_unchanged(before, after)


async def test_real_run_set_lifecycle_response_carries_dry_run_false(
    graph_store, lifecycle_service
):
    """Positive control: real-run sets dry_run=False on the response.
    Confirms the echo discriminates real-run from dry-run; a bug that
    always returned dry_run=True would let downstream callers
    misinterpret real writes as previews."""
    doc = _make_doc(_id("doc_real_run"))
    await graph_store.insert_document(doc)

    response = await lifecycle_service.set_lifecycle(
        _id("doc_real_run"),
        SetLifecycleRequest(action="archive"),
    )
    assert response.dry_run is False


async def test_real_run_supersede_response_carries_created_edge(graph_store, lifecycle_service):
    """Positive control: real-run supersede populates created_edge with
    a non-sentinel id (the actual UUID assigned at commit). Confirms
    created_edge is uniformly populated on supersede regardless of
    dry-run, per the planned semantics."""
    doc_old = _make_doc(_id("doc_real_old"))
    doc_new = _make_doc(_id("doc_real_new"))
    await graph_store.insert_document(doc_old)
    await graph_store.insert_document(doc_new)

    response = await lifecycle_service.set_lifecycle(
        _id("doc_real_old"),
        SetLifecycleRequest(action="supersede", new_version_id=_id("doc_real_new")),
    )
    assert response.dry_run is False
    assert response.created_edge is not None
    assert response.created_edge.id != DRY_RUN_SENTINEL_EDGE_ID
    assert response.created_edge.source_id == _id("doc_real_new")
    assert response.created_edge.target_id == _id("doc_real_old")
    # And the edge is queryable from storage.
    edges = await graph_store.get_edges_by_source(_id("doc_real_new"), "supersedes")
    assert len(edges) == 1
    assert edges[0].id == response.created_edge.id


# ---------------------------------------------------------------------------
# (B) Same-validator paired
# ---------------------------------------------------------------------------


async def test_invalid_action_envelope_identical_under_dry_run(graph_store, lifecycle_service):
    """Both paths raise InvalidActionError with identical envelope."""
    doc = _make_doc(_id("doc_invalid_action"))
    await graph_store.insert_document(doc)

    with pytest.raises(InvalidActionError) as real_info:
        await lifecycle_service.set_lifecycle(
            _id("doc_invalid_action"), SetLifecycleRequest(action="nonexistent")
        )
    with pytest.raises(InvalidActionError) as dry_info:
        await lifecycle_service.set_lifecycle(
            _id("doc_invalid_action"),
            SetLifecycleRequest(action="nonexistent", dry_run=True),
        )

    assert real_info.value.code == dry_info.value.code
    assert real_info.value.detail == dry_info.value.detail


async def test_invalid_transition_envelope_identical_under_dry_run(graph_store, lifecycle_service):
    """Both paths raise InvalidLifecycleTransitionError with the same
    valid_actions list."""
    doc = _make_doc(_id("doc_invalid_trans"), lifecycle_status="archived")
    await graph_store.insert_document(doc)
    await graph_store.update_document(_id("doc_invalid_trans"), {"lifecycle_status": "archived"})

    with pytest.raises(InvalidLifecycleTransitionError) as real_info:
        await lifecycle_service.set_lifecycle(
            _id("doc_invalid_trans"), SetLifecycleRequest(action="complete")
        )
    with pytest.raises(InvalidLifecycleTransitionError) as dry_info:
        await lifecycle_service.set_lifecycle(
            _id("doc_invalid_trans"),
            SetLifecycleRequest(action="complete", dry_run=True),
        )

    assert real_info.value.code == dry_info.value.code
    assert real_info.value.detail == dry_info.value.detail


async def test_document_not_found_envelope_identical_under_dry_run(lifecycle_service):
    """Both paths raise DocumentNotFoundError for a missing target id."""
    missing = _id("doc_nope")

    with pytest.raises(DocumentNotFoundError) as real_info:
        await lifecycle_service.set_lifecycle(missing, SetLifecycleRequest(action="archive"))
    with pytest.raises(DocumentNotFoundError) as dry_info:
        await lifecycle_service.set_lifecycle(
            missing, SetLifecycleRequest(action="archive", dry_run=True)
        )

    assert real_info.value.code == dry_info.value.code
    assert real_info.value.detail == dry_info.value.detail


async def test_supersede_missing_new_version_envelope_identical_under_dry_run(
    graph_store, lifecycle_service
):
    """Both paths raise MissingFieldError for supersede without new_version_id."""
    doc = _make_doc(_id("doc_no_new_v"))
    await graph_store.insert_document(doc)

    with pytest.raises(MissingFieldError) as real_info:
        await lifecycle_service.set_lifecycle(
            _id("doc_no_new_v"), SetLifecycleRequest(action="supersede")
        )
    with pytest.raises(MissingFieldError) as dry_info:
        await lifecycle_service.set_lifecycle(
            _id("doc_no_new_v"),
            SetLifecycleRequest(action="supersede", dry_run=True),
        )

    assert real_info.value.code == dry_info.value.code
    assert real_info.value.detail == dry_info.value.detail


async def test_supersede_missing_new_version_doc_envelope_identical_under_dry_run(
    graph_store, lifecycle_service
):
    """Both paths raise DocumentNotFoundError for a missing new_version_id."""
    doc = _make_doc(_id("doc_super_b"))
    await graph_store.insert_document(doc)
    missing_new = _id("doc_missing_new")

    with pytest.raises(DocumentNotFoundError) as real_info:
        await lifecycle_service.set_lifecycle(
            _id("doc_super_b"),
            SetLifecycleRequest(action="supersede", new_version_id=missing_new),
        )
    with pytest.raises(DocumentNotFoundError) as dry_info:
        await lifecycle_service.set_lifecycle(
            _id("doc_super_b"),
            SetLifecycleRequest(action="supersede", new_version_id=missing_new, dry_run=True),
        )

    assert real_info.value.code == dry_info.value.code
    assert real_info.value.detail == dry_info.value.detail


# ---------------------------------------------------------------------------
# (C) Side-effect-specific
# ---------------------------------------------------------------------------


async def test_dry_run_does_not_call_update_chunk_metadata(
    graph_store, lifecycle_service, stub_content_store
):
    """Anti-coincidental: dry-run must not touch chunk pushdown fields.
    Positive control: real-run does. Catches the bug where dry-run
    accidentally syncs chunks after skipping the document write."""
    doc = _make_doc(_id("doc_chunk_probe"))
    await graph_store.insert_document(doc)

    before = await state_snapshot(graph_store, stub_content_store)
    await lifecycle_service.set_lifecycle(
        _id("doc_chunk_probe"),
        SetLifecycleRequest(action="archive", dry_run=True),
    )
    after_dry = await state_snapshot(graph_store, stub_content_store)
    assert_state_unchanged(before, after_dry)

    # Positive control: real-run flips lifecycle_status (in the doc row;
    # the stub content store records the chunk update too if there were
    # chunks).
    await lifecycle_service.set_lifecycle(
        _id("doc_chunk_probe"),
        SetLifecycleRequest(action="archive"),
    )
    after_real = await state_snapshot(graph_store, stub_content_store)
    assert after_real.documents[_id("doc_chunk_probe")]["lifecycle_status"] == "archived"


async def test_supersede_dry_run_persists_no_edge(
    graph_store, lifecycle_service, stub_content_store
):
    """Anti-coincidental: supersede dry-run must persist NO supersedes
    edge. The supersede branch uses supersede_atomic which bundles the
    lifecycle flip and the edge insert; a buggy dry-run could skip the
    flip but still insert the edge, or vice versa."""
    doc_old = _make_doc(_id("doc_no_edge_old"))
    doc_new = _make_doc(_id("doc_no_edge_new"))
    await graph_store.insert_document(doc_old)
    await graph_store.insert_document(doc_new)

    before = await state_snapshot(graph_store, stub_content_store)

    await lifecycle_service.set_lifecycle(
        _id("doc_no_edge_old"),
        SetLifecycleRequest(
            action="supersede",
            new_version_id=_id("doc_no_edge_new"),
            dry_run=True,
        ),
    )

    after = await state_snapshot(graph_store, stub_content_store)
    assert_state_unchanged(before, after)
    edges = await graph_store.get_edges_by_source(_id("doc_no_edge_new"), "supersedes")
    assert edges == []
