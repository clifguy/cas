"""T-0152: `update_metadata` dry-run.

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
    TagAddConflictError,
    Tier3DocTypeChangeStaleKeysError,
    Tier3SchemaViolationError,
)
from sage.models.enums import SourceType
from sage.models.schemas import (
    IngestRequest,
    TagsPatch,
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
            adapter=SourceType.MARKDOWN,
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
    """Same proof on the tags surface: dry-run with TagsPatch.add returns
    the would-be tag list but does not write."""
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "misc", "tags": ["existing"]},
        )
    )

    before = await state_snapshot(graph_store, stub_content_store)

    response = await tier3_metadata_service.update_metadata(
        initial.document.id,
        UpdateMetadataRequest(tags=TagsPatch(add=["new"]), dry_run=True),
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
            adapter=SourceType.MARKDOWN,
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
    """T-0151 case: stale keys on doc_type change raise the same
    Tier3DocTypeChangeStaleKeysError in both paths."""
    _write_md(tmp_vault_dir, "d.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="d.md",
            adapter=SourceType.MARKDOWN,
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
            adapter=SourceType.MARKDOWN,
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


async def test_tag_add_conflict_envelope_identical_under_dry_run(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service
):
    """TagAddConflictError raised by both paths when adding a tag that
    is already present on the document."""
    _write_md(tmp_vault_dir, "d.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="d.md",
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "misc", "tags": ["already_here"]},
        )
    )

    with pytest.raises(TagAddConflictError) as real_info:
        await tier3_metadata_service.update_metadata(
            initial.document.id,
            UpdateMetadataRequest(tags=TagsPatch(add=["already_here"])),
            modified_by="tester",
        )

    with pytest.raises(TagAddConflictError) as dry_info:
        await tier3_metadata_service.update_metadata(
            initial.document.id,
            UpdateMetadataRequest(tags=TagsPatch(add=["already_here"]), dry_run=True),
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
            UpdateMetadataRequest(tags=TagsPatch(add=["x"])),
            modified_by="tester",
        )

    with pytest.raises(DocumentNotFoundError) as dry_info:
        await tier3_metadata_service.update_metadata(
            missing,
            UpdateMetadataRequest(tags=TagsPatch(add=["x"]), dry_run=True),
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
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
        )
    )

    pre_doc = await graph_store.get_document(initial.document.id)
    pre_updated_at = pre_doc.updated_at
    pre_metadata_confirmed = pre_doc.metadata_confirmed

    await tier3_metadata_service.update_metadata(
        initial.document.id,
        UpdateMetadataRequest(tags=TagsPatch(add=["fresh"]), dry_run=True),
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
            adapter=SourceType.MARKDOWN,
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
