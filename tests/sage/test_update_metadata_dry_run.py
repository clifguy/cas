"""`update_metadata` dry-run.

Three test categories per the plan:

A. Happy-path dry-run — response carries the would-be document and
   `dry_run=True`; state fingerprint is unchanged.
B. Same-validator paired — identical inputs that hit a known error;
   real-run and dry-run produce the same error envelope (proves "same
   validators in the same order").
C. Side-effect-specific — `update_chunk_metadata` not called;
   `metadata_confirmed` and `updated_at` unchanged on dry-run, both
   advance on real-run (positive control).
"""

# ruff: noqa: F811
# Pytest fixture-share pattern: the tier3_* fixtures are imported by
# name from test_tier3_metadata to make them visible to pytest's
# fixture-resolution machinery, then re-used as per-test parameters.
# Ruff sees each parameter as a redefinition of the imported name; in
# pytest the import IS the fixture-registration mechanism, so the
# pattern is canonical and the F811 flag is a known false positive.

from __future__ import annotations

import pytest

from sage.api.errors import (
    DocumentNotFoundError,
    InvalidDocTypeError,
    ListFieldAddConflictError,
    Tier3DocTypeChangeStaleKeysError,
    Tier3SchemaViolationError,
)
from sage.models.enums import SourceType
from sage.models.schemas import (
    IngestRequest,
    ListFieldPatch,
    Tier3Patch,
    UpdateMetadataRequest,
    UpdateMetadataResponse,
)
from tests.sage._dry_run_helpers import assert_state_unchanged, state_snapshot
from tests.sage.test_tier3_metadata import (  # noqa: F401 -- fixtures
    _write_md,
    tier3_config,
    tier3_ingestion_service,
    tier3_metadata_service,
)

# ---------------------------------------------------------------------------
# (A) Happy-path dry-run
# ---------------------------------------------------------------------------


async def test_dry_run_returns_post_patch_document_without_writing(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store, stub_content_store
):
    """Dry-run patches in memory: response carries the would-be tier3
    state and `dry_run=True`; storage and chunk-pushdown state are
    byte-identical to pre-call."""
    _write_md(tmp_vault_dir, "fr.md", "# Failure\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="fr.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "failure_record"},
            tier3_metadata={"severity": "low", "fix_commit": "abc123"},
        )
    )

    before = await state_snapshot(graph_store, stub_content_store)

    response = await tier3_metadata_service.update_metadata(
        initial.document.id,
        UpdateMetadataRequest(
            tier3_metadata=Tier3Patch(set={"severity": "high"}),
            dry_run=True,
        ),
        modified_by="tester",
    )

    after = await state_snapshot(graph_store, stub_content_store)

    assert isinstance(response, UpdateMetadataResponse)
    assert response.dry_run is True
    # Would-be tier3 state surfaces in response.document.
    assert response.document.tier3_metadata == {"severity": "high", "fix_commit": "abc123"}
    # And no state actually changed.
    assert_state_unchanged(before, after)


async def test_dry_run_with_tags_patch_does_not_persist(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store, stub_content_store
):
    """Same proof on the tags surface: dry-run with ListFieldPatch.add returns
    the would-be tag list but does not write."""
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc", "tags": ["existing"]},
        )
    )

    before = await state_snapshot(graph_store, stub_content_store)

    response = await tier3_metadata_service.update_metadata(
        initial.document.id,
        UpdateMetadataRequest(tags=ListFieldPatch(add=["new"]), dry_run=True),
        modified_by="tester",
    )

    after = await state_snapshot(graph_store, stub_content_store)

    assert response.dry_run is True
    assert sorted(response.document.tags) == ["existing", "new"]
    assert_state_unchanged(before, after)


# ---------------------------------------------------------------------------
# (B) Same-validator paired — identical inputs, identical errors
# ---------------------------------------------------------------------------


async def test_tier3_schema_violation_envelope_identical_under_dry_run(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service
):
    """Tier3SchemaViolationError raised by both real and dry runs with
    the same error.code and detail. Proves the schema validator runs in
    the dry-run path."""
    _write_md(tmp_vault_dir, "t.md", "# Ticket\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="t.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001"},
        )
    )

    real_exc: Tier3SchemaViolationError | None = None
    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await tier3_metadata_service.update_metadata(
            initial.document.id,
            UpdateMetadataRequest(tier3_metadata=Tier3Patch(set={"ticket_id": "BAD"})),
            modified_by="tester",
        )
    real_exc = excinfo.value

    dry_exc: Tier3SchemaViolationError | None = None
    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await tier3_metadata_service.update_metadata(
            initial.document.id,
            UpdateMetadataRequest(
                tier3_metadata=Tier3Patch(set={"ticket_id": "BAD"}),
                dry_run=True,
            ),
            modified_by="tester",
        )
    dry_exc = excinfo.value

    assert real_exc.code == dry_exc.code
    assert real_exc.detail == dry_exc.detail


async def test_tier3_doc_type_change_stale_keys_envelope_identical_under_dry_run(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service
):
    """case: stale keys on doc_type change raise the same
    Tier3DocTypeChangeStaleKeysError in both paths."""
    _write_md(tmp_vault_dir, "d.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="d.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "high"},
        )
    )

    with pytest.raises(Tier3DocTypeChangeStaleKeysError) as real_info:
        await tier3_metadata_service.update_metadata(
            initial.document.id,
            UpdateMetadataRequest(
                doc_type="failure_record",
                tier3_metadata=Tier3Patch(set={"severity": "high"}),
            ),
            modified_by="tester",
        )

    with pytest.raises(Tier3DocTypeChangeStaleKeysError) as dry_info:
        await tier3_metadata_service.update_metadata(
            initial.document.id,
            UpdateMetadataRequest(
                doc_type="failure_record",
                tier3_metadata=Tier3Patch(set={"severity": "high"}),
                dry_run=True,
            ),
            modified_by="tester",
        )

    assert real_info.value.code == dry_info.value.code
    assert real_info.value.detail == dry_info.value.detail


async def test_invalid_doc_type_envelope_identical_under_dry_run(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service
):
    """InvalidDocTypeError raised by both paths when doc_type is not in
    the vault config's vocabulary."""
    _write_md(tmp_vault_dir, "d.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="d.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
        )
    )

    with pytest.raises(InvalidDocTypeError) as real_info:
        await tier3_metadata_service.update_metadata(
            initial.document.id,
            UpdateMetadataRequest(doc_type="not_in_config"),
            modified_by="tester",
        )

    with pytest.raises(InvalidDocTypeError) as dry_info:
        await tier3_metadata_service.update_metadata(
            initial.document.id,
            UpdateMetadataRequest(doc_type="not_in_config", dry_run=True),
            modified_by="tester",
        )

    assert real_info.value.code == dry_info.value.code
    assert real_info.value.detail == dry_info.value.detail


async def test_tags_add_conflict_envelope_identical_under_dry_run(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service
):
    """ListFieldAddConflictError raised by both paths when adding a tag that
    is already present on the document."""
    _write_md(tmp_vault_dir, "d.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="d.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc", "tags": ["already_here"]},
        )
    )

    with pytest.raises(ListFieldAddConflictError) as real_info:
        await tier3_metadata_service.update_metadata(
            initial.document.id,
            UpdateMetadataRequest(tags=ListFieldPatch(add=["already_here"])),
            modified_by="tester",
        )

    with pytest.raises(ListFieldAddConflictError) as dry_info:
        await tier3_metadata_service.update_metadata(
            initial.document.id,
            UpdateMetadataRequest(tags=ListFieldPatch(add=["already_here"]), dry_run=True),
            modified_by="tester",
        )

    assert real_info.value.code == dry_info.value.code
    assert real_info.value.detail == dry_info.value.detail


async def test_document_not_found_envelope_identical_under_dry_run(tier3_metadata_service):
    """DocumentNotFoundError raised by both paths when the target id
    does not exist. The lookup is the first thing the service does, so
    both paths surface this identically."""
    import hashlib

    missing = "{}_missing".format(hashlib.sha256(b"missing").hexdigest()[:8])

    with pytest.raises(DocumentNotFoundError) as real_info:
        await tier3_metadata_service.update_metadata(
            missing,
            UpdateMetadataRequest(tags=ListFieldPatch(add=["x"])),
            modified_by="tester",
        )

    with pytest.raises(DocumentNotFoundError) as dry_info:
        await tier3_metadata_service.update_metadata(
            missing,
            UpdateMetadataRequest(tags=ListFieldPatch(add=["x"]), dry_run=True),
            modified_by="tester",
        )

    assert real_info.value.code == dry_info.value.code
    assert real_info.value.detail == dry_info.value.detail


# ---------------------------------------------------------------------------
# (C) Side-effect-specific — chunk store, metadata_confirmed, updated_at
# ---------------------------------------------------------------------------


async def test_dry_run_does_not_advance_updated_at_or_metadata_confirmed(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """Anti-coincidental: catch the bug where the service stamps
    updated_at and flips metadata_confirmed BEFORE the dry-run branch."""
    _write_md(tmp_vault_dir, "d.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="d.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
        )
    )

    pre_doc = await graph_store.get_document(initial.document.id)
    pre_updated_at = pre_doc.updated_at
    pre_metadata_confirmed = pre_doc.metadata_confirmed

    await tier3_metadata_service.update_metadata(
        initial.document.id,
        UpdateMetadataRequest(tags=ListFieldPatch(add=["fresh"]), dry_run=True),
        modified_by="tester",
    )

    post_doc = await graph_store.get_document(initial.document.id)
    assert post_doc.updated_at == pre_updated_at, "dry-run must not advance updated_at"
    assert post_doc.metadata_confirmed == pre_metadata_confirmed, (
        "dry-run must not flip metadata_confirmed"
    )


async def test_dry_run_does_not_call_update_chunk_metadata(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store, stub_content_store
):
    """Anti-coincidental: when the dry-run patches doc_type or project,
    the chunk-store sync must not run (it would mutate chunk pushdown
    fields). Real-run does call it (positive control)."""
    _write_md(tmp_vault_dir, "d.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="d.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
        )
    )

    before = await state_snapshot(graph_store, stub_content_store)
    await tier3_metadata_service.update_metadata(
        initial.document.id,
        UpdateMetadataRequest(project="new_project", dry_run=True),
        modified_by="tester",
    )
    after_dry = await state_snapshot(graph_store, stub_content_store)
    assert_state_unchanged(before, after_dry)

    # Positive control: same call without dry_run DOES change chunk
    # metadata + document row. Asserts the dry-run gate is the only
    # thing suppressing the writes; the underlying machinery works.
    await tier3_metadata_service.update_metadata(
        initial.document.id,
        UpdateMetadataRequest(project="new_project"),
        modified_by="tester",
    )
    after_real = await state_snapshot(graph_store, stub_content_store)
    # Document row changed (project + updated_at + metadata_confirmed).
    assert after_real.documents[initial.document.id]["project"] == "new_project"
    # Chunk pushdown changed.
    assert after_real.chunk_metadata != before.chunk_metadata


# ---------------------------------------------------------------------------
# (D) `changes` block — dry-run deltas
# ---------------------------------------------------------------------------


async def test_dry_run_changes_lists_scalar_deltas(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store, stub_content_store
):
    """Dry-run with scalar patches surfaces one FieldChange per
    changed field with the bare field name as `path` and the actual
    pre/post values as before/after."""
    _write_md(tmp_vault_dir, "scalars.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="scalars.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc", "project": "alpha", "title": "Old"},
        )
    )

    before = await state_snapshot(graph_store, stub_content_store)
    response = await tier3_metadata_service.update_metadata(
        initial.document.id,
        UpdateMetadataRequest(title="New", project="beta", dry_run=True),
        modified_by="tester",
    )
    after = await state_snapshot(graph_store, stub_content_store)

    assert response.dry_run is True
    assert response.changes is not None
    # Sorted by path for determinism: project < title.
    paths = [c.path for c in response.changes]
    assert paths == ["project", "title"]
    by_path = {c.path: c for c in response.changes}
    assert by_path["title"].before == "Old"
    assert by_path["title"].after == "New"
    assert by_path["project"].before == "alpha"
    assert by_path["project"].after == "beta"
    assert_state_unchanged(before, after)


async def test_dry_run_changes_enumerates_tier3_per_key(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store, stub_content_store
):
    """Tier3 changes enumerate per-key with dotted paths. An
    unset key surfaces as `after=None`; a set-to-new-value surfaces with
    the pre value in `before`. Lumping the whole tier3 dict into one
    entry would fail this test."""
    _write_md(tmp_vault_dir, "fr.md", "# FR\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="fr.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "failure_record"},
            tier3_metadata={"severity": "low", "fix_commit": "abc123"},
        )
    )

    before = await state_snapshot(graph_store, stub_content_store)
    response = await tier3_metadata_service.update_metadata(
        initial.document.id,
        UpdateMetadataRequest(
            tier3_metadata=Tier3Patch(set={"severity": "high"}, unset=["fix_commit"]),
            dry_run=True,
        ),
        modified_by="tester",
    )
    after = await state_snapshot(graph_store, stub_content_store)

    assert response.dry_run is True
    assert response.changes is not None
    by_path = {c.path: c for c in response.changes}
    # Per-key entries with dotted paths, not a single tier3_metadata entry.
    assert set(by_path.keys()) == {
        "tier3_metadata.severity",
        "tier3_metadata.fix_commit",
    }
    assert by_path["tier3_metadata.severity"].before == "low"
    assert by_path["tier3_metadata.severity"].after == "high"
    assert by_path["tier3_metadata.fix_commit"].before == "abc123"
    assert by_path["tier3_metadata.fix_commit"].after is None
    # Sorted by path.
    assert [c.path for c in response.changes] == [
        "tier3_metadata.fix_commit",
        "tier3_metadata.severity",
    ]
    assert_state_unchanged(before, after)


async def test_dry_run_changes_tags_uses_full_before_after_lists(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store, stub_content_store
):
    """Tags change uses full ordered before/after lists, not the
    patch ops shape (so callers don't have to round-trip the patch
    semantics to compute the post-state)."""
    _write_md(tmp_vault_dir, "tagdoc.md", "# Tagdoc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="tagdoc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc", "tags": ["a", "b"]},
        )
    )

    before = await state_snapshot(graph_store, stub_content_store)
    response = await tier3_metadata_service.update_metadata(
        initial.document.id,
        UpdateMetadataRequest(tags=ListFieldPatch(add=["c"], remove=["a"]), dry_run=True),
        modified_by="tester",
    )
    after = await state_snapshot(graph_store, stub_content_store)

    assert response.dry_run is True
    assert response.changes is not None
    assert len(response.changes) == 1
    change = response.changes[0]
    assert change.path == "tags"
    # Full lists, not the patch shape: before is the existing ordered
    # list, after is the post-patch ordered list (survivors + appends).
    assert change.before == ["a", "b"]
    assert change.after == ["b", "c"]
    assert_state_unchanged(before, after)


async def test_real_run_changes_block_absent(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """Real-run responses carry `changes=None`. A non-None
    `changes` value unambiguously means 'this was a dry-run.'"""
    _write_md(tmp_vault_dir, "realrun.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="realrun.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc", "title": "Old"},
        )
    )

    response = await tier3_metadata_service.update_metadata(
        initial.document.id,
        UpdateMetadataRequest(title="New"),  # dry_run defaults to False
        modified_by="tester",
    )
    assert response.dry_run is False
    assert response.changes is None
    # Sanity: real-run still applied the change.
    assert response.document.title == "New"


async def test_dry_run_empty_actionable_patch_changes_block_is_none(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store, stub_content_store
):
    """A dry-run that updates no caller-supplied fields (e.g., a
    bare-confirmation call with no scalars or patches) carries
    `changes=None`, matching the real-run-absence pattern. Codifies the
    `None` choice for the empty-changes boundary."""
    _write_md(tmp_vault_dir, "empty.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="empty.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
        )
    )

    before = await state_snapshot(graph_store, stub_content_store)
    response = await tier3_metadata_service.update_metadata(
        initial.document.id,
        UpdateMetadataRequest(dry_run=True),
        modified_by="tester",
    )
    after = await state_snapshot(graph_store, stub_content_store)

    assert response.dry_run is True
    # No caller-supplied field changes → `changes is None`, not [].
    assert response.changes is None
    assert_state_unchanged(before, after)
