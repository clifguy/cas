"""Service-layer tests for MetadataService.bulk_update_metadata (T-0088).

The bulk method holds the per-document lock per item and the per-item
SQLite transaction; the batch as a whole is NOT atomic. A bad item does
not roll back earlier-or-later successful items (CAS-ADR-029). Patch
semantics per item match CAS-ADR-028 single-item rules.
"""

from __future__ import annotations

import pytest

from sage.config import VaultConfig
from sage.models.schemas import (
    BulkMetadataItem,
    BulkMetadataRequest,
    TagsPatch,
    Tier3Patch,
)
from sage.services.metadata import MetadataService
from tests.sage.test_lifecycle import _id, _make_doc
from tests.sage.test_tier3_metadata import _config_dict_with_tier3


@pytest.fixture
def bulk_tier3_config(tmp_vault_dir):
    return VaultConfig.model_validate(_config_dict_with_tier3(tmp_vault_dir))


@pytest.fixture
def bulk_metadata_service(graph_store, lock_manager, bulk_tier3_config, stub_content_store):
    return MetadataService(graph_store, lock_manager, bulk_tier3_config, stub_content_store)


async def _insert_with_state(
    graph_store,
    doc_id: str,
    *,
    doc_type: str | None = None,
    tags: list[str] | None = None,
    tier3_metadata: dict | None = None,
    metadata_confirmed: bool = True,
):
    """Seed a document with non-default tags/tier3/doc_type."""
    await graph_store.insert_document(_make_doc(doc_id))
    updates: dict = {"metadata_confirmed": metadata_confirmed}
    if doc_type is not None:
        updates["doc_type"] = doc_type
    if tags is not None:
        updates["tags"] = tags
    if tier3_metadata is not None:
        updates["tier3_metadata"] = tier3_metadata
    await graph_store.update_document(doc_id, updates)


# ---------------------------------------------------------------------------
# 1. Happy path with scalar + tag + tier3 changes (anti-coincidental-pass:
#    re-read every doc from storage).
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_happy_path_scalar_and_tag_changes(
    graph_store, bulk_metadata_service
):
    ids = [_id("doc_h1"), _id("doc_h2"), _id("doc_h3")]
    for doc_id in ids:
        await _insert_with_state(
            graph_store,
            doc_id,
            doc_type="ticket",
            tags=["a"],
            tier3_metadata={"ticket_id": "T-0001"},
        )

    response = await bulk_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(
                    document_id=doc_id,
                    title=f"renamed {doc_id}",
                    tags=TagsPatch(add=["b"]),
                    tier3_metadata=Tier3Patch(set={"ticket_priority": "high"}),
                )
                for doc_id in ids
            ]
        ),
        modified_by="testuser",
    )

    assert response.total == 3
    assert response.success_count == 3
    assert response.error_count == 0
    for entry, doc_id in zip(response.results, ids, strict=True):
        assert entry.status == "success"
        assert entry.document_id == doc_id
        assert entry.document is not None
        assert entry.document.title == f"renamed {doc_id}"
        assert entry.document.tags == ["a", "b"]
        assert entry.document.tier3_metadata == {
            "ticket_id": "T-0001",
            "ticket_priority": "high",
        }
        assert entry.document.metadata_confirmed is True
        assert entry.error is None

    # Anti-coincidental-pass: re-read each document from storage. A
    # naive implementation that synthesizes response records without
    # writing to the graph store would fail here.
    for doc_id in ids:
        stored = await graph_store.get_document(doc_id)
        assert stored.tags == ["a", "b"]
        assert stored.tier3_metadata == {"ticket_id": "T-0001", "ticket_priority": "high"}
        assert stored.title == f"renamed {doc_id}"


# ---------------------------------------------------------------------------
# 2. Empty items list.
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_empty_items_returns_empty_response(bulk_metadata_service):
    response = await bulk_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(items=[]),
        modified_by="testuser",
    )
    assert response.total == 0
    assert response.success_count == 0
    assert response.error_count == 0
    assert response.results == []


# ---------------------------------------------------------------------------
# 3. Mixed valid/invalid: per-item error envelopes; neighbors still commit.
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_mixed_valid_invalid_partial_success(
    graph_store, bulk_metadata_service
):
    ids = [_id("doc_p1"), _id("doc_p2"), _id("doc_p3")]
    # Seed each doc with tags=["already_present"] so item 1's add-conflict
    # is deterministic; items 0 and 2 add a new tag and succeed.
    for doc_id in ids:
        await _insert_with_state(
            graph_store,
            doc_id,
            doc_type="ticket",
            tags=["already_present"],
            tier3_metadata={"ticket_id": "T-0001"},
        )

    response = await bulk_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(document_id=ids[0], tags=TagsPatch(add=["x"])),
                BulkMetadataItem(document_id=ids[1], tags=TagsPatch(add=["already_present"])),
                BulkMetadataItem(document_id=ids[2], tags=TagsPatch(add=["y"])),
            ]
        ),
        modified_by="testuser",
    )

    assert response.total == 3
    assert response.success_count == 2
    assert response.error_count == 1

    assert response.results[0].status == "success"
    assert response.results[1].status == "error"
    assert response.results[1].document_id == ids[1]
    assert response.results[1].error["error"] == "tag_add_conflict"
    assert "document_id" in response.results[1].error["detail"]
    assert "current_tags" in response.results[1].error["detail"]
    assert response.results[2].status == "success"

    # Anti-coincidental-pass: neighbors must show the new tag persisted
    # even though item 1 raised. A batch-wide-transaction implementation
    # would roll items 0 and 2 back when item 1 conflicted.
    stored0 = await graph_store.get_document(ids[0])
    stored2 = await graph_store.get_document(ids[2])
    assert "x" in stored0.tags
    assert "y" in stored2.tags
    # The failing item's tags are unchanged.
    stored1 = await graph_store.get_document(ids[1])
    assert stored1.tags == ["already_present"]


# ---------------------------------------------------------------------------
# 4. tier3 unset of absent key surfaces tier3_unset_conflict per-item.
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_tier3_unset_conflict_per_item_error(
    graph_store, bulk_metadata_service
):
    doc_id = _id("doc_t3_unset")
    await _insert_with_state(
        graph_store,
        doc_id,
        doc_type="ticket",
        tier3_metadata={"ticket_id": "T-0001"},
    )

    response = await bulk_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(
                    document_id=doc_id,
                    tier3_metadata=Tier3Patch(unset=["absent_key"]),
                )
            ]
        ),
        modified_by="testuser",
    )

    assert response.success_count == 0
    assert response.error_count == 1
    entry = response.results[0]
    assert entry.status == "error"
    assert entry.error["error"] == "tier3_unset_conflict"
    detail = entry.error["detail"]
    assert detail["document_id"] == doc_id
    assert detail["doc_type"] == "ticket"
    assert "absent_key" in detail["keys"]
    assert "current_tier3_keys" in detail
    # Storage unchanged.
    stored = await graph_store.get_document(doc_id)
    assert stored.tier3_metadata == {"ticket_id": "T-0001"}


# ---------------------------------------------------------------------------
# 5. Post-merge tier3 schema violation surfaces tier3_schema_violation.
#    Mutation probe: removing _validate_tier3 makes this test pass with
#    the bad merged dict committing.
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_tier3_schema_violation_per_item_error(
    graph_store, bulk_metadata_service
):
    doc_id = _id("doc_t3_schema")
    await _insert_with_state(
        graph_store,
        doc_id,
        doc_type="ticket",
        tier3_metadata={"ticket_id": "T-0001"},
    )

    response = await bulk_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(
                    document_id=doc_id,
                    # 'ticket_priority' must be one of high/medium/low per
                    # the test vault's metadata_schema; 'purple' violates.
                    tier3_metadata=Tier3Patch(set={"ticket_priority": "purple"}),
                )
            ]
        ),
        modified_by="testuser",
    )

    assert response.error_count == 1
    entry = response.results[0]
    assert entry.status == "error"
    assert entry.error["error"] == "tier3_schema_violation"
    # Storage state for that doc remains the pre-call state — the bad
    # merged dict must NOT commit.
    stored = await graph_store.get_document(doc_id)
    assert stored.tier3_metadata == {"ticket_id": "T-0001"}


# ---------------------------------------------------------------------------
# 5b. T-0151: doc_type change paired with tier3 ops in one bulk item
#     surfaces tier3_doc_type_change_stale_keys per-item; neighbor commits.
#     Anti-coincidental: storage-probe on the failing doc confirms it is
#     unchanged; the new error must fire pre-write.
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_doc_type_change_stale_keys_per_item_error(
    graph_store, bulk_metadata_service
):
    doc_a = _id("doc_t151_a")
    doc_b = _id("doc_t151_b")
    await _insert_with_state(
        graph_store,
        doc_a,
        doc_type="ticket",
        tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "high"},
    )
    await _insert_with_state(
        graph_store,
        doc_b,
        doc_type="ticket",
        tier3_metadata={"ticket_id": "T-0002"},
    )

    response = await bulk_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                # Reclassifying without unsetting the legacy keys -- should fail.
                BulkMetadataItem(
                    document_id=doc_a,
                    doc_type="failure_record",
                    tier3_metadata=Tier3Patch(set={"severity": "high"}),
                ),
                # Neighbor with only a tier3 patch -- should succeed.
                BulkMetadataItem(
                    document_id=doc_b,
                    tier3_metadata=Tier3Patch(set={"ticket_priority": "low"}),
                ),
            ]
        ),
        modified_by="testuser",
    )

    assert response.error_count == 1
    assert response.success_count == 1
    failing = response.results[0]
    assert failing.status == "error"
    assert failing.error["error"] == "tier3_doc_type_change_stale_keys"
    assert failing.error["detail"]["stale_keys"] == ["ticket_id", "ticket_priority"]
    assert failing.error["detail"]["previous_doc_type"] == "ticket"
    assert failing.error["detail"]["new_doc_type"] == "failure_record"

    # Anti-coincidental storage probes.
    stored_a = await graph_store.get_document(doc_a)
    assert stored_a.doc_type == "ticket"
    assert stored_a.tier3_metadata == {"ticket_id": "T-0001", "ticket_priority": "high"}
    stored_b = await graph_store.get_document(doc_b)
    assert stored_b.tier3_metadata == {"ticket_id": "T-0002", "ticket_priority": "low"}


# ---------------------------------------------------------------------------
# 5c. T-0156: doc_type change with no Tier3Patch and stale stored keys
#     surfaces tier3_doc_type_change_stale_keys per-item; neighbor commits.
#     Locks in parity with the single-item path -- bulk_update_metadata is
#     a thin loop over update_metadata.
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_doc_type_change_no_tier3_patch_stale_keys_per_item_error(
    graph_store, bulk_metadata_service
):
    doc_a = _id("doc_t156_a")
    doc_b = _id("doc_t156_b")
    await _insert_with_state(
        graph_store,
        doc_a,
        doc_type="ticket",
        tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "high"},
    )
    await _insert_with_state(
        graph_store,
        doc_b,
        doc_type="ticket",
        tier3_metadata={"ticket_id": "T-0002"},
    )

    response = await bulk_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                # T-0156 Wart 1: doc_type change with no Tier3Patch but
                # stale stored keys -- must reject.
                BulkMetadataItem(
                    document_id=doc_a,
                    doc_type="failure_record",
                ),
                # Neighbor with only a tier3 patch -- should succeed.
                BulkMetadataItem(
                    document_id=doc_b,
                    tier3_metadata=Tier3Patch(set={"ticket_priority": "low"}),
                ),
            ]
        ),
        modified_by="testuser",
    )

    assert response.error_count == 1
    assert response.success_count == 1
    failing = response.results[0]
    assert failing.status == "error"
    assert failing.error["error"] == "tier3_doc_type_change_stale_keys"
    assert failing.error["detail"]["stale_keys"] == ["ticket_id", "ticket_priority"]
    assert failing.error["detail"]["previous_doc_type"] == "ticket"
    assert failing.error["detail"]["new_doc_type"] == "failure_record"

    # Anti-coincidental storage probes.
    stored_a = await graph_store.get_document(doc_a)
    assert stored_a.doc_type == "ticket"
    assert stored_a.tier3_metadata == {"ticket_id": "T-0001", "ticket_priority": "high"}
    stored_b = await graph_store.get_document(doc_b)
    assert stored_b.tier3_metadata == {"ticket_id": "T-0002", "ticket_priority": "low"}


# ---------------------------------------------------------------------------
# 5d. T-0156 Wart 2: doc_type change to no-schema target with Tier3Patch
#     that unsets every legacy key (empty merged dict) succeeds per-item.
#     A bad neighbor confirms per-item isolation.
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_doc_type_change_to_no_schema_unset_all_per_item_success(
    graph_store, bulk_metadata_service
):
    doc_ok = _id("doc_t156_w2_ok")
    doc_bad = _id("doc_t156_w2_bad")
    await _insert_with_state(
        graph_store,
        doc_ok,
        doc_type="ticket",
        tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "high"},
    )
    await _insert_with_state(
        graph_store,
        doc_bad,
        doc_type="ticket",
        tier3_metadata={"ticket_id": "T-0002", "ticket_priority": "high"},
    )

    response = await bulk_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                # Reclassify to no-schema doc_type while unsetting every
                # legacy key -- T-0156 Wart 2 should let this through.
                BulkMetadataItem(
                    document_id=doc_ok,
                    doc_type="misc",
                    tier3_metadata=Tier3Patch(unset=["ticket_id", "ticket_priority"]),
                ),
                # Bad neighbor: reclassify without unsetting -- stale keys
                # rejection per item, isolation from the success above.
                BulkMetadataItem(
                    document_id=doc_bad,
                    doc_type="misc",
                ),
            ]
        ),
        modified_by="testuser",
    )

    assert response.success_count == 1
    assert response.error_count == 1
    assert response.results[0].status == "success"
    assert response.results[1].status == "error"
    assert response.results[1].error["error"] == "tier3_doc_type_change_stale_keys"

    # Anti-coincidental storage probes.
    stored_ok = await graph_store.get_document(doc_ok)
    assert stored_ok.doc_type == "misc"
    assert stored_ok.tier3_metadata == {}
    stored_bad = await graph_store.get_document(doc_bad)
    assert stored_bad.doc_type == "ticket"
    assert stored_bad.tier3_metadata == {"ticket_id": "T-0002", "ticket_priority": "high"}


# ---------------------------------------------------------------------------
# 6. Unknown document_id surfaces document_not_found per-item; neighbor
#    still commits.
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_unknown_document_id_per_item_error(
    graph_store, bulk_metadata_service
):
    real = _id("doc_real")
    ghost = _id("doc_ghost")
    await _insert_with_state(graph_store, real, doc_type="ticket", tags=["a"])

    response = await bulk_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(document_id=real, tags=TagsPatch(add=["b"])),
                BulkMetadataItem(document_id=ghost, tags=TagsPatch(add=["b"])),
            ]
        ),
        modified_by="testuser",
    )

    assert response.success_count == 1
    assert response.error_count == 1
    assert response.results[0].status == "success"
    assert response.results[1].status == "error"
    assert response.results[1].error["error"] == "document_not_found"

    # Anti-coincidental-pass: the real doc must have the new tag persisted.
    stored = await graph_store.get_document(real)
    assert stored.tags == ["a", "b"]


# ---------------------------------------------------------------------------
# 7. Invalid doc_type surfaces invalid_doc_type per-item; neighbor commits.
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_invalid_doc_type_per_item_error(
    graph_store, bulk_metadata_service
):
    a = _id("doc_dt_a")
    b = _id("doc_dt_b")
    await _insert_with_state(graph_store, a, doc_type="ticket", tags=["a"])
    await _insert_with_state(graph_store, b, doc_type="ticket", tags=["a"])

    response = await bulk_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(document_id=a, tags=TagsPatch(add=["b"])),
                BulkMetadataItem(document_id=b, doc_type="not_in_vault_config"),
            ]
        ),
        modified_by="testuser",
    )

    assert response.success_count == 1
    assert response.error_count == 1
    assert response.results[1].error["error"] == "invalid_doc_type"
    # Anti-coincidental-pass: neighbor commits.
    stored_a = await graph_store.get_document(a)
    assert stored_a.tags == ["a", "b"]
    # b is unchanged.
    stored_b = await graph_store.get_document(b)
    assert stored_b.doc_type == "ticket"


# ---------------------------------------------------------------------------
# 8. Duplicate document_id serializes via per-doc lock; updated_at is
#    monotonically non-decreasing across the three items.
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_duplicate_document_id_serializes_via_per_doc_lock(
    graph_store, bulk_metadata_service
):
    doc_id = _id("doc_dup")
    await _insert_with_state(graph_store, doc_id, doc_type="ticket", tags=[])

    response = await bulk_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(document_id=doc_id, tags=TagsPatch(add=["a"])),
                BulkMetadataItem(document_id=doc_id, tags=TagsPatch(add=["b"])),
                BulkMetadataItem(document_id=doc_id, tags=TagsPatch(add=["c"])),
            ]
        ),
        modified_by="testuser",
    )

    assert response.success_count == 3
    assert response.error_count == 0

    # Final stored tags reflect the three additions in order.
    stored = await graph_store.get_document(doc_id)
    assert stored.tags == ["a", "b", "c"]

    # Anti-coincidental-pass: per-document lock means the three updates
    # are serialized; each result.document.updated_at is monotonically
    # non-decreasing. A bug that drops the lock would interleave the
    # writes and produce out-of-order timestamps.
    updated_ats = [r.document.updated_at for r in response.results]
    assert updated_ats == sorted(updated_ats), (
        f"updated_at sequence must be monotonic across serialized "
        f"per-document updates; got {updated_ats!r}"
    )


# ---------------------------------------------------------------------------
# 9. Per-item transactions — load-bearing isolation test. Mutation probe:
#    a batch-wide try/except would roll item 0 back when item 1 raises.
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_per_item_transactions_no_batch_rollback(
    graph_store, bulk_metadata_service
):
    real = _id("doc_iso_real")
    ghost = _id("doc_iso_ghost")
    await _insert_with_state(graph_store, real, doc_type="ticket", tags=["a"])

    response = await bulk_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(document_id=real, tags=TagsPatch(add=["b"])),
                BulkMetadataItem(document_id=ghost, tags=TagsPatch(add=["b"])),
            ]
        ),
        modified_by="testuser",
    )

    assert response.results[0].status == "success"
    assert response.results[1].status == "error"

    # Anti-coincidental-pass: re-read item 0 directly from storage. A
    # batch-wide-transaction implementation would roll item 0 back when
    # item 1 raised; per-item transaction isolation is the load-bearing
    # contract.
    stored = await graph_store.get_document(real)
    assert stored.tags == ["a", "b"], (
        "item 0 must remain committed even though item 1 raised; "
        "per-item transaction isolation is the load-bearing contract."
    )


# ---------------------------------------------------------------------------
# 10. Each successful patch sets metadata_confirmed=True (CAS-ADR-021
#     behavior carries through the bulk path).
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_sets_metadata_confirmed_on_each_success(
    graph_store, bulk_metadata_service
):
    ids = [_id("doc_mc1"), _id("doc_mc2"), _id("doc_mc3")]
    for doc_id in ids:
        await _insert_with_state(
            graph_store,
            doc_id,
            doc_type="ticket",
            tags=["a"],
            metadata_confirmed=False,
        )

    response = await bulk_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(document_id=doc_id, tags=TagsPatch(add=["b"])) for doc_id in ids
            ]
        ),
        modified_by="testuser",
    )

    assert response.success_count == 3
    for entry in response.results:
        assert entry.document.metadata_confirmed is True

    # Anti-coincidental-pass: re-read from storage.
    for doc_id in ids:
        stored = await graph_store.get_document(doc_id)
        assert stored.metadata_confirmed is True
