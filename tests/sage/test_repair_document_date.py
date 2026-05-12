"""Tests for the repair-document-date one-shot script.

Exercises the service-level entry point ``repair_with_services`` against
a real graph store fixture. The script normalizes ``document_date``
values that were persisted in ISO-with-time form (e.g.
``2026-05-05T00:00:00Z``) back to the schema-contract YYYY-MM-DD shape.

Defensive contract: dry-run is the default and writes nothing; truly
unparseable values are reported but not heuristically rewritten; clean
records are not touched.
"""

from datetime import datetime, timezone

from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document
from scripts.repair_document_date import repair_with_services


def _make_doc(doc_id: str, document_date: str | None) -> Document:
    # model_construct is required: this helper deliberately seeds storage with
    # malformed document_date values to exercise the repair workflow, which
    # the typed-alias validator would reject if construction ran through it.
    now = datetime.now(timezone.utc)
    return Document.model_construct(
        id=doc_id,
        title=f"Doc {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        source_content_hash=f"hash_{doc_id}",
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
        document_date=document_date,
    )


async def _seed_mixed_dates(graph_store):
    """Three docs: clean, malformed (ISO-with-Z), null."""
    await graph_store.insert_document(_make_doc("clean", "2026-05-04"))
    await graph_store.insert_document(_make_doc("malformed", "2026-05-05T00:00:00Z"))
    await graph_store.insert_document(_make_doc("null_date", None))


async def test_repair_dry_run_reports_targets_without_writing(graph_store):
    """Dry-run plans the rewrite for the malformed record but the DB is unchanged."""
    await _seed_mixed_dates(graph_store)

    result = await repair_with_services(graph=graph_store, execute=False)

    targets = [(t.doc_id, t.new_value) for t in result.targets]
    assert ("malformed", "2026-05-05") in targets
    assert not any(t[0] == "clean" for t in targets)
    assert not any(t[0] == "null_date" for t in targets)

    after = await graph_store.get_document("malformed")
    assert after.document_date == "2026-05-05T00:00:00Z"


async def test_repair_execute_rewrites_malformed_records(graph_store):
    """With execute=True the malformed record's document_date is normalized."""
    await _seed_mixed_dates(graph_store)

    result = await repair_with_services(graph=graph_store, execute=True)

    assert result.rewrites_applied == 1

    malformed_after = await graph_store.get_document("malformed")
    assert malformed_after.document_date == "2026-05-05"

    clean_after = await graph_store.get_document("clean")
    assert clean_after.document_date == "2026-05-04"

    null_after = await graph_store.get_document("null_date")
    assert null_after.document_date is None


async def test_repair_skips_unparseable_without_raising(graph_store):
    """A truly malformed document_date is reported but not heuristically rewritten."""
    await graph_store.insert_document(_make_doc("garbage", "not a date"))

    result = await repair_with_services(graph=graph_store, execute=True)

    assert result.rewrites_applied == 0
    assert any(s.doc_id == "garbage" for s in result.skipped)

    after = await graph_store.get_document("garbage")
    assert after.document_date == "not a date"
