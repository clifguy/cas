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

from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH
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
    chunks = await ingestion_service._content_store.get_all_chunks(document_id)
    header_chunks = [c for c in chunks if c.heading_path == SYNTHETIC_HEADER_HEADING_PATH]
    assert len(header_chunks) == 1
    assert "A repaired abstract." in header_chunks[0].content


async def test_update_semantic_abstract_missing_document_raises(ingestion_service):
    """A phantom document id raises instead of silently succeeding.

    Kills a silent no-op, which would let a bulk repair pass report
    success for a document it never touched.
    """
    with pytest.raises(DocumentNotFoundError):
        await ingestion_service.update_semantic_abstract("no-such-doc", "An abstract.")
