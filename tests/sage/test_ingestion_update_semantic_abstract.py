"""In-place semantic-abstract replacement on the ingestion service.

The CAS-ADR-020 clause (e) backfill repairs stored abstracts without
regeneration: under greedy decoding a regeneration reproduces the same
breached output, so the repaired text is written directly. The
load-bearing property tested here is retrieval coherence -- a rewritten
abstract must reach the synthetic header chunk, or hybrid retrieval
keeps serving the pre-repair text it indexed at ingest time.
"""

from pathlib import Path

import pytest

from sage.api.errors import DocumentNotFoundError
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest


def _create_test_file(
    tmp_vault_dir: Path, relative_path: str, content: str = "# Test\n\nTest content."
) -> Path:
    full_path = tmp_vault_dir / "sources" / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content)
    return full_path


async def test_update_semantic_abstract_persists_and_refreshes_header_chunk(
    tmp_vault_dir, graph_store, ingestion_service
):
    """The new abstract lands in the document row and the header chunk.

    The header-chunk assertion is the anti-coincidental half: an
    implementation that writes the document row but skips the header
    refresh passes the row assertion while retrieval keeps serving the
    replaced abstract forever.
    """
    _create_test_file(tmp_vault_dir, "reports/repair_target.md")
    request = IngestRequest(source="reports/repair_target.md", source_type=SourceType.MARKDOWN)
    result = await ingestion_service.ingest(request)
    document_id = result.document.id

    await ingestion_service.update_semantic_abstract(document_id, "A repaired abstract.")

    doc = await graph_store.get_document(document_id)
    assert doc.semantic_abstract == "A repaired abstract."
    # The abstract is derived text and lives on the document surface, where
    # it orients and ranks without becoming matchable (CAS-ADR-049).
    surface = ingestion_service._content_store.stored_document_surface(document_id)
    assert surface is not None, "the repair must refresh the document surface"
    assert "A repaired abstract." in surface.orienting
    assert "A repaired abstract." not in surface.matchable, (
        "a generated abstract must not reach the matchable half"
    )


async def test_update_semantic_abstract_missing_document_raises(ingestion_service):
    """A phantom document id raises instead of silently succeeding.

    Kills a silent no-op, which would let a bulk repair pass report
    success for a document it never touched.
    """
    with pytest.raises(DocumentNotFoundError):
        await ingestion_service.update_semantic_abstract("no-such-doc", "An abstract.")


async def test_repair_preserves_served_lineage_after_unsuccessful_replacement(
    ingestion_service, monkeypatch
):
    from tests.sage.test_bounded_abstraction import package
    from tests.sage.test_postgres_graph_store import _doc

    service = ingestion_service
    doc = _doc(1)
    await service._store.insert_document(doc)
    first = await service._content_store.begin_processing_attempt(doc.id)

    async def generate(*args, **kwargs):
        return "The orchard grows apples."

    monkeypatch.setattr(service._abstraction, "generate_abstract", generate)
    await service._abstract_package(doc.id, None, first, package())
    generated = await service._store.get_document(doc.id)
    assert generated.processing_coverage.served_abstraction_generation == first
    assert generated.processing_coverage.abstraction_evidence.generation == first
    await service.update_semantic_abstract(doc.id, "The orchard grows apples with care.")
    repaired = await service._store.get_document(doc.id)
    assert repaired.processing_coverage == generated.processing_coverage

    newer = await service._content_store.begin_processing_attempt(doc.id)
    service._config.abstraction.max_input_tokens = 2000
    await service._abstract_package(doc.id, None, newer, package(fields=2))
    before = await service._store.get_document(doc.id)
    assert before.processing_coverage.abstraction_evidence.counts[0].represented == 0
    assert before.processing_coverage.served_abstraction_generation == first
    await service.update_semantic_abstract(
        doc.id, "The orchard grows apples carefully.", expected_abstract=before.semantic_abstract
    )
    after = await service._store.get_document(doc.id)
    assert after.processing_coverage == before.processing_coverage
    assert after.semantic_abstract == "The orchard grows apples carefully."
    # A repair planned from the former text cannot overwrite the replacement.
    with pytest.raises(ValueError, match="changed since repair planning"):
        await service.update_semantic_abstract(
            doc.id, "Stale repair.", expected_abstract=before.semantic_abstract
        )
    assert (await service._store.get_document(doc.id)).semantic_abstract == after.semantic_abstract


async def test_legacy_text_repair_keeps_unknown_provenance(ingestion_service):
    from tests.sage.test_postgres_graph_store import _doc

    service = ingestion_service
    doc = _doc(1).model_copy(update={"semantic_abstract": "Legacy description."})
    await service._store.insert_document(doc)
    await service.update_semantic_abstract(doc.id, "Repaired legacy description.")
    after = await service._store.get_document(doc.id)
    assert after.processing_coverage == doc.processing_coverage
    assert after.processing_coverage.served_abstraction_generation is None
    assert after.semantic_abstract == "Repaired legacy description."


@pytest.mark.parametrize("race", ["new_text", "new_attempt"])
async def test_repair_refuses_change_after_service_snapshot(ingestion_service, monkeypatch, race):
    from tests.sage.test_postgres_graph_store import _doc

    service = ingestion_service
    doc = _doc(1).model_copy(update={"semantic_abstract": "Original description."})
    await service._store.insert_document(doc)
    publish = service._content_store.publish_processing

    async def raced(*args, **kwargs):
        if race == "new_attempt":
            await service._content_store.begin_processing_attempt(doc.id)
        else:
            await service._store.update_document(
                doc.id,
                {"semantic_abstract": "Concurrent description."},
                advance_version=False,
                advance_processing_attempt=False,
            )
        return await publish(*args, **kwargs)

    monkeypatch.setattr(service._content_store, "publish_processing", raced)
    with pytest.raises(ValueError, match="changed during repair"):
        await service.update_semantic_abstract(doc.id, "Stale replacement.")
    after = await service._store.get_document(doc.id)
    assert after.semantic_abstract == (
        "Concurrent description." if race == "new_text" else "Original description."
    )


async def test_text_repair_contract_in_non_delegating_stub(ingestion_service):
    from tests.sage.test_postgres_graph_store import _doc

    service = ingestion_service
    doc = _doc(1).model_copy(update={"semantic_abstract": "Original description."})
    from sage.adapters.stubs import StubGraphStore

    service._store = StubGraphStore()
    await service._store.insert_document(doc)
    service._content_store.bind_processing_store(service._store)
    await service.update_semantic_abstract(doc.id, "Repaired description.")
    after = await service._store.get_document(doc.id)
    assert after.processing_coverage == doc.processing_coverage
    assert after.semantic_abstract == "Repaired description."
