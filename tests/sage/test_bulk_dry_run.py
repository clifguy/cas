"""Bulk-tool dry-run plumbing (`bulk_update_metadata`, `bulk_set_lifecycle`).

Three test categories per the plan:

D. Envelope echo — bulk response carries `dry_run=True`; per-item
   document bodies are populated on success.
E. Per-item independence (documented limitation) — item N adds tag X,
   item N+1 also adds tag X to the same document. Real run: N succeeds,
   N+1 raises TagAddConflictError. Dry run: both report success because
   each item's dry-run is evaluated against the committed state at
   batch start; no item's would-be effects are visible to subsequent
   items.
F. Mixed-result dry-run — batch with some passing and some failing
   items; per-item envelope shape is identical between real and dry
   runs.
"""

# ruff: noqa: F811
# Pytest fixture-share pattern: the tier3_* fixtures are imported by
# name from test_tier3_metadata to make them visible to pytest's
# fixture-resolution machinery, then re-used as per-test parameters.
# Ruff sees each parameter as a redefinition of the imported name; in
# pytest the import IS the fixture-registration mechanism, so the
# pattern is canonical and the F811 flag is a known false positive.

from __future__ import annotations

from sage.models.enums import ResponseMode, SourceType
from sage.models.schemas import (
    BulkLifecycleItem,
    BulkLifecycleRequest,
    BulkMetadataItem,
    BulkMetadataRequest,
    IngestRequest,
    TagsPatch,
)
from tests.sage._dry_run_helpers import assert_state_unchanged, state_snapshot
from tests.sage.test_lifecycle import _id, _make_doc
from tests.sage.test_tier3_metadata import (  # noqa: F401 -- fixtures
    _write_md,
    tier3_config,
    tier3_ingestion_service,
    tier3_metadata_service,
)

# ---------------------------------------------------------------------------
# (D) Envelope echo
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_envelope_echoes_dry_run(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store, stub_content_store
):
    """BulkMetadataResponse.dry_run echoes the envelope; per-item
    documents carry post-state under FULL mode; state is unchanged."""
    _write_md(tmp_vault_dir, "a.md", "# A\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="a.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc", "tags": ["seed"]},
        )
    )

    before = await state_snapshot(graph_store, stub_content_store)

    response = await tier3_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(
                    document_id=initial.document.id,
                    tags=TagsPatch(add=["new_tag"]),
                )
            ],
            response_mode=ResponseMode.FULL,
            dry_run=True,
        ),
        modified_by="tester",
    )

    after = await state_snapshot(graph_store, stub_content_store)

    assert response.dry_run is True
    assert response.success_count == 1
    assert response.results[0].status == "success"
    assert sorted(response.results[0].document.tags) == ["new_tag", "seed"]
    assert_state_unchanged(before, after)


async def test_bulk_set_lifecycle_envelope_echoes_dry_run(
    graph_store, lifecycle_service, stub_content_store
):
    """BulkLifecycleResponse.dry_run echoes the envelope; state unchanged."""
    doc = _make_doc(_id("doc_bulk_lc"))
    await graph_store.insert_document(doc)

    before = await state_snapshot(graph_store, stub_content_store)

    response = await lifecycle_service.bulk_set_lifecycle(
        BulkLifecycleRequest(
            items=[BulkLifecycleItem(document_id=_id("doc_bulk_lc"), action="archive")],
            response_mode=ResponseMode.FULL,
            dry_run=True,
        )
    )

    after = await state_snapshot(graph_store, stub_content_store)

    assert response.dry_run is True
    assert response.success_count == 1
    assert response.results[0].status == "success"
    assert response.results[0].document.lifecycle_status == "archived"  # would-be
    assert_state_unchanged(before, after)


# ---------------------------------------------------------------------------
# (E) Per-item independence — documented limitation
# ---------------------------------------------------------------------------


async def test_bulk_metadata_dry_run_does_not_simulate_prior_item_effects(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store, stub_content_store
):
    """Documented limitation: each per-item dry-run is evaluated
    against the committed state at batch start; one item's would-be
    effects are NOT visible to subsequent items.

    Scenario: item N adds tag X; item N+1 tries to add the same tag
    X to the same document.
    - Real-run: N succeeds (X is added), N+1 raises tag_add_conflict.
    - Dry-run: BOTH report success because the per-item dry-run sees
      the original (pre-N) tag set when validating N+1.

    This test guards against accidental "smarter" behavior (a
    shadow-store simulating predecessor effects) which would drift
    from the documented contract."""
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
        )
    )
    doc_id = initial.document.id

    # Dry-run: both add-X items pass, because neither sees the
    # would-be effects of the prior item.
    dry_response = await tier3_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(document_id=doc_id, tags=TagsPatch(add=["shared"])),
                BulkMetadataItem(document_id=doc_id, tags=TagsPatch(add=["shared"])),
            ],
            response_mode=ResponseMode.FULL,
            dry_run=True,
        ),
        modified_by="tester",
    )
    assert dry_response.dry_run is True
    assert dry_response.success_count == 2
    assert dry_response.error_count == 0

    # Real-run: item 0 succeeds, item 1 fails because tag is now present.
    real_response = await tier3_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(document_id=doc_id, tags=TagsPatch(add=["fresh"])),
                BulkMetadataItem(document_id=doc_id, tags=TagsPatch(add=["fresh"])),
            ],
            response_mode=ResponseMode.FULL,
        ),
        modified_by="tester",
    )
    assert real_response.dry_run is False
    assert real_response.success_count == 1
    assert real_response.error_count == 1
    assert real_response.results[1].status == "error"
    assert real_response.results[1].error["error"] == "tag_add_conflict"


# ---------------------------------------------------------------------------
# (F) Mixed-result dry-run — same envelope shape as real run
# ---------------------------------------------------------------------------


async def test_bulk_metadata_dry_run_mixed_results_same_shape_as_real(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store, stub_content_store
):
    """Batch with a valid and an invalid item. Real and dry runs
    produce identical per-item envelope shapes (status, error
    payload). State is unchanged after the dry-run; the valid item is
    NOT committed."""
    _write_md(tmp_vault_dir, "good.md", "# Good\n\nBody.")
    good = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="good.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
        )
    )
    missing_id = "ffffffff_does_not_exist"

    before = await state_snapshot(graph_store, stub_content_store)

    dry = await tier3_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(document_id=good.document.id, tags=TagsPatch(add=["t1"])),
                BulkMetadataItem(document_id=missing_id, tags=TagsPatch(add=["t1"])),
            ],
            response_mode=ResponseMode.FULL,
            dry_run=True,
        ),
        modified_by="tester",
    )

    after = await state_snapshot(graph_store, stub_content_store)

    assert dry.dry_run is True
    assert dry.success_count == 1
    assert dry.error_count == 1
    assert dry.results[0].status == "success"
    assert dry.results[1].status == "error"
    assert dry.results[1].error["error"] == "document_not_found"
    assert_state_unchanged(before, after)


# ---------------------------------------------------------------------------
# (G) `changes` block — per-item dry-run deltas
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_dry_run_per_item_changes_populated(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store, stub_content_store
):
    """Each per-item dry-run success carries a `changes` block
    derived from that item's patch. Item N's changes match item N's
    patch, not item N+1's — verifies the bulk wrapper passes the
    per-item changes through, not a shared/lumped value."""
    _write_md(tmp_vault_dir, "alpha.md", "# Alpha\n\nBody.")
    _write_md(tmp_vault_dir, "beta.md", "# Beta\n\nBody.")
    alpha = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="alpha.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc", "title": "Alpha Old", "tags": ["a"]},
        )
    )
    beta = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="beta.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc", "project": "old_proj"},
        )
    )

    before = await state_snapshot(graph_store, stub_content_store)

    response = await tier3_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(
                    document_id=alpha.document.id,
                    title="Alpha New",
                ),
                BulkMetadataItem(
                    document_id=beta.document.id,
                    project="new_proj",
                ),
            ],
            response_mode=ResponseMode.FULL,
            dry_run=True,
        ),
        modified_by="tester",
    )

    after = await state_snapshot(graph_store, stub_content_store)

    assert response.dry_run is True
    assert response.success_count == 2
    # Item 0 changes only the title; item 1 changes only the project.
    assert response.results[0].changes is not None
    assert len(response.results[0].changes) == 1
    assert response.results[0].changes[0].path == "title"
    assert response.results[0].changes[0].before == "Alpha Old"
    assert response.results[0].changes[0].after == "Alpha New"
    assert response.results[1].changes is not None
    assert len(response.results[1].changes) == 1
    assert response.results[1].changes[0].path == "project"
    assert response.results[1].changes[0].before == "old_proj"
    assert response.results[1].changes[0].after == "new_proj"
    assert_state_unchanged(before, after)


async def test_bulk_set_lifecycle_dry_run_per_item_changes_populated(
    graph_store, lifecycle_service, stub_content_store
):
    """Each per-item lifecycle dry-run carries a one-entry
    `changes` block for `lifecycle_status`."""
    doc_a = _make_doc(_id("doc_bulk_changes_a"))
    doc_b = _make_doc(_id("doc_bulk_changes_b"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    before = await state_snapshot(graph_store, stub_content_store)
    response = await lifecycle_service.bulk_set_lifecycle(
        BulkLifecycleRequest(
            items=[
                BulkLifecycleItem(document_id=_id("doc_bulk_changes_a"), action="archive"),
                BulkLifecycleItem(document_id=_id("doc_bulk_changes_b"), action="complete"),
            ],
            response_mode=ResponseMode.FULL,
            dry_run=True,
        )
    )
    after = await state_snapshot(graph_store, stub_content_store)

    assert response.dry_run is True
    assert response.success_count == 2
    # Item 0: archive — lifecycle_status: active -> archived.
    assert response.results[0].changes is not None
    assert len(response.results[0].changes) == 1
    assert response.results[0].changes[0].path == "lifecycle_status"
    assert response.results[0].changes[0].before == "active"
    assert response.results[0].changes[0].after == "archived"
    # Item 1: complete — lifecycle_status: active -> completed.
    assert response.results[1].changes is not None
    assert len(response.results[1].changes) == 1
    assert response.results[1].changes[0].path == "lifecycle_status"
    assert response.results[1].changes[0].before == "active"
    assert response.results[1].changes[0].after == "completed"
    assert_state_unchanged(before, after)


async def test_bulk_dry_run_changes_preserved_under_response_mode_light(
    graph_store, lifecycle_service, stub_content_store
):
    """`changes` is preserved under `response_mode=light`. With
    a 6-item batch the default is LIGHT (LIGHT_DEFAULT_THRESHOLD=5), so
    per-item `document` is dropped but `changes` survives — it's small
    and is the most useful summary for dry-run callers.

    Anti-coincidental: the test asserts `document is None` in the same
    place, proving light mode actually fired (the changes-preservation
    claim would be vacuous if light mode silently downgraded to FULL)."""
    docs = []
    for i in range(6):
        d = _make_doc(_id(f"doc_light_{i}"))
        await graph_store.insert_document(d)
        docs.append(d)

    response = await lifecycle_service.bulk_set_lifecycle(
        BulkLifecycleRequest(
            items=[
                BulkLifecycleItem(document_id=_id(f"doc_light_{i}"), action="archive")
                for i in range(6)
            ],
            # response_mode=None → default-resolution rule applies; 6 > 5 → LIGHT.
            dry_run=True,
        )
    )

    assert response.dry_run is True
    assert response.success_count == 6
    for item in response.results:
        # Light mode dropped the document body (proves light fired).
        assert item.document is None
        # But changes survived — that's the contract.
        assert item.changes is not None
        assert len(item.changes) == 1
        assert item.changes[0].path == "lifecycle_status"
        assert item.changes[0].after == "archived"


async def test_bulk_set_lifecycle_real_run_per_item_changes_absent(
    graph_store, lifecycle_service, stub_content_store
):
    """Real-run bulk lifecycle responses carry `changes=None`
    on every per-item result."""
    doc = _make_doc(_id("doc_bulk_realrun"))
    await graph_store.insert_document(doc)

    response = await lifecycle_service.bulk_set_lifecycle(
        BulkLifecycleRequest(
            items=[BulkLifecycleItem(document_id=_id("doc_bulk_realrun"), action="archive")],
            response_mode=ResponseMode.FULL,
        )
    )

    assert response.dry_run is False
    assert response.results[0].changes is None
    # Sanity: real-run actually applied the change.
    assert response.results[0].document.lifecycle_status == "archived"


async def test_bulk_update_metadata_real_run_per_item_changes_absent(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """Real-run bulk metadata responses carry `changes=None`
    on every per-item result."""
    _write_md(tmp_vault_dir, "real.md", "# Real\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="real.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
        )
    )

    response = await tier3_metadata_service.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                BulkMetadataItem(document_id=initial.document.id, tags=TagsPatch(add=["realtag"]))
            ],
            response_mode=ResponseMode.FULL,
        ),
        modified_by="tester",
    )

    assert response.dry_run is False
    assert response.results[0].changes is None
    # Sanity: real-run actually applied the change.
    assert "realtag" in response.results[0].document.tags
