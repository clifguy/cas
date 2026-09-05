"""Service-layer tests for LifecycleService.bulk_set_lifecycle.

The bulk method holds the per-document lock per item and the per-item
database transaction; the batch as a whole is NOT atomic. A bad item does
not roll back earlier-or-later successful items (CAS-ADR-029).
"""

from __future__ import annotations

from sage.models.enums import PipelineStatus
from sage.models.schemas import (
    BulkLifecycleItem,
    BulkLifecycleRequest,
)
from tests.sage.test_lifecycle import _id, _make_doc


async def test_bulk_set_lifecycle_happy_path_homogeneous_action(graph_store, lifecycle_service):
    """Three active docs, all archived in one call; aggregate counts and
    persisted state both correct."""
    ids = [_id("doc_h1"), _id("doc_h2"), _id("doc_h3")]
    for doc_id in ids:
        await graph_store.insert_document(_make_doc(doc_id))

    response = await lifecycle_service.bulk_set_lifecycle(
        BulkLifecycleRequest(
            items=[BulkLifecycleItem(document_id=d, action="archive") for d in ids]
        )
    )

    assert response.total == 3
    assert response.success_count == 3
    assert response.error_count == 0
    assert len(response.results) == 3
    for entry, doc_id in zip(response.results, ids, strict=True):
        assert entry.status == "success"
        assert entry.document_id == doc_id
        assert entry.document is not None
        assert entry.document.lifecycle_status == "archived"
        assert entry.error is None

    # Anti-coincidental-pass: re-read each document from storage to
    # confirm the response is not lying about what was persisted.
    for doc_id in ids:
        stored = await graph_store.get_document(doc_id)
        assert stored.lifecycle_status == "archived"


async def test_bulk_set_lifecycle_empty_items_returns_empty_response(lifecycle_service):
    """Empty list is a valid, no-op input."""
    response = await lifecycle_service.bulk_set_lifecycle(BulkLifecycleRequest(items=[]))

    assert response.total == 0
    assert response.success_count == 0
    assert response.error_count == 0
    assert response.results == []


async def test_bulk_set_lifecycle_partial_success_on_invalid_action(graph_store, lifecycle_service):
    """A bad item in the middle does not roll back its neighbours."""
    ids = [_id("doc_p1"), _id("doc_p2"), _id("doc_p3")]
    for doc_id in ids:
        await graph_store.insert_document(_make_doc(doc_id))

    response = await lifecycle_service.bulk_set_lifecycle(
        BulkLifecycleRequest(
            items=[
                BulkLifecycleItem(document_id=ids[0], action="archive"),
                BulkLifecycleItem(document_id=ids[1], action="frobnicate"),
                BulkLifecycleItem(document_id=ids[2], action="archive"),
            ]
        )
    )

    assert response.total == 3
    assert response.success_count == 2
    assert response.error_count == 1

    assert response.results[0].status == "success"
    assert response.results[1].status == "error"
    assert response.results[1].document_id == ids[1]
    assert response.results[1].error is not None
    assert response.results[1].error["error"] == "invalid_action"
    assert response.results[2].status == "success"

    # Anti-coincidental-pass: the two valid items must be persisted as
    # archived (independent of the failing middle item).
    assert (await graph_store.get_document(ids[0])).lifecycle_status == "archived"
    assert (await graph_store.get_document(ids[2])).lifecycle_status == "archived"
    # The failing item stays in the active state it started in.
    assert (await graph_store.get_document(ids[1])).lifecycle_status == "active"


async def test_bulk_set_lifecycle_partial_success_on_unknown_document(
    graph_store, lifecycle_service
):
    """Targeting a non-existent document_id surfaces document_not_found and
    the rest of the batch continues."""
    real = _id("doc_real")
    ghost = _id("doc_ghost")
    await graph_store.insert_document(_make_doc(real))

    response = await lifecycle_service.bulk_set_lifecycle(
        BulkLifecycleRequest(
            items=[
                BulkLifecycleItem(document_id=real, action="archive"),
                BulkLifecycleItem(document_id=ghost, action="archive"),
            ]
        )
    )

    assert response.success_count == 1
    assert response.error_count == 1
    assert response.results[0].status == "success"
    assert response.results[1].status == "error"
    assert response.results[1].error["error"] == "document_not_found"

    # Anti-coincidental-pass: real doc must be persisted as archived.
    assert (await graph_store.get_document(real)).lifecycle_status == "archived"


async def test_bulk_set_lifecycle_supersede_with_missing_new_version_returns_per_item_error(
    graph_store, lifecycle_service
):
    """A supersede whose successor_id does not exist surfaces per-item
    error without leaking out of the batch."""
    pred = _id("doc_pred")
    await graph_store.insert_document(_make_doc(pred))

    response = await lifecycle_service.bulk_set_lifecycle(
        BulkLifecycleRequest(
            items=[
                BulkLifecycleItem(
                    document_id=pred,
                    action="supersede",
                    successor_id=_id("nonexistent_successor"),
                )
            ]
        )
    )

    assert response.error_count == 1
    assert response.results[0].status == "error"
    assert response.results[0].error["error"] == "document_not_found"
    # Anti-coincidental-pass: predecessor remains active.
    assert (await graph_store.get_document(pred)).lifecycle_status == "active"


async def test_bulk_set_lifecycle_duplicate_document_id_serializes_via_per_doc_lock(
    graph_store, lifecycle_service
):
    """Three items targeting the same document_id serialize in order via
    the per-document lock; final state and timestamp monotonicity match."""
    doc_id = _id("doc_dup")
    await graph_store.insert_document(_make_doc(doc_id))

    response = await lifecycle_service.bulk_set_lifecycle(
        BulkLifecycleRequest(
            items=[
                BulkLifecycleItem(document_id=doc_id, action="archive"),
                BulkLifecycleItem(document_id=doc_id, action="reactivate"),
                BulkLifecycleItem(document_id=doc_id, action="archive"),
            ]
        )
    )

    assert response.success_count == 3
    assert response.error_count == 0
    states = [r.document.lifecycle_status for r in response.results]
    assert states == ["archived", "active", "archived"]

    # Final stored state matches the last successful transition.
    assert (await graph_store.get_document(doc_id)).lifecycle_status == "archived"

    # Anti-coincidental-pass: per-document lock means the three updates
    # are serialized; each updated_at is monotonically non-decreasing.
    updated_ats = [r.document.updated_at for r in response.results]
    assert updated_ats == sorted(updated_ats), (
        f"updated_at sequence must be monotonic across serialized transitions; got {updated_ats!r}"
    )


async def test_bulk_set_lifecycle_warnings_pass_through(graph_store, lifecycle_service):
    """A non-terminal pipeline_status produces the same advisory warning
    the single-item path emits."""
    doc_id = _id("doc_indexing")
    await graph_store.insert_document(
        _make_doc(doc_id, pipeline_status=PipelineStatus.INDEXING_IN_PROGRESS)
    )

    response = await lifecycle_service.bulk_set_lifecycle(
        BulkLifecycleRequest(items=[BulkLifecycleItem(document_id=doc_id, action="archive")])
    )

    assert response.success_count == 1
    entry = response.results[0]
    assert entry.status == "success"
    assert entry.warnings is not None
    assert len(entry.warnings) == 1
    assert "pipeline" in entry.warnings[0]


async def test_bulk_set_lifecycle_complete_with_nonterminal_pipeline_succeeds_with_warning(
    graph_store, lifecycle_service
):
    """`complete` is not gated on a terminal pipeline_status.

    The per-item validation surface inherits whatever the single-item
    path enforces, and a non-terminal pipeline is not part of it: the
    item succeeds, the document rests in `completed`, and the advisory
    warning is the whole of the response's account of the running
    pipeline. Covered here as well as at the single-item level because
    both surfaces describe this precondition set to a caller, and a
    caller reads the bulk one.
    """
    doc_id = _id("doc_indexing_bulk_complete")
    await graph_store.insert_document(
        _make_doc(doc_id, pipeline_status=PipelineStatus.INDEXING_IN_PROGRESS)
    )

    response = await lifecycle_service.bulk_set_lifecycle(
        BulkLifecycleRequest(items=[BulkLifecycleItem(document_id=doc_id, action="complete")])
    )

    assert response.success_count == 1
    assert response.error_count == 0
    entry = response.results[0]
    assert entry.status == "success"
    assert entry.error is None
    assert entry.document is not None
    assert entry.document.lifecycle_status == "completed"
    assert entry.document.pipeline_status == PipelineStatus.INDEXING_IN_PROGRESS
    assert entry.warnings is not None
    assert len(entry.warnings) == 1
    assert "pipeline" in entry.warnings[0]

    # Anti-coincidental-pass: re-read from storage, so a response that
    # reports a transition it did not persist cannot carry this test.
    stored = await graph_store.get_document(doc_id)
    assert stored.lifecycle_status == "completed"


async def test_bulk_set_lifecycle_distinct_items_run_per_item_transactions(
    graph_store, lifecycle_service
):
    """Item 1's commit must persist even when item 2 raises a SAGEError;
    proves each item runs in its own database transaction (no batch-wide
    rollback)."""
    real = _id("doc_iso_real")
    ghost = _id("doc_iso_ghost")
    await graph_store.insert_document(_make_doc(real))

    response = await lifecycle_service.bulk_set_lifecycle(
        BulkLifecycleRequest(
            items=[
                BulkLifecycleItem(document_id=real, action="archive"),
                BulkLifecycleItem(document_id=ghost, action="archive"),
            ]
        )
    )

    # Item 2 fails with document_not_found; item 1's commit must survive.
    assert response.results[0].status == "success"
    assert response.results[1].status == "error"

    # Anti-coincidental-pass: re-read item 1 directly from storage. A
    # batch-wide-transaction implementation would roll item 1 back when
    # item 2 raised.
    stored = await graph_store.get_document(real)
    assert stored.lifecycle_status == "archived", (
        "item 1 must remain committed even though item 2 raised; per-item "
        "transaction isolation is the load-bearing contract."
    )
