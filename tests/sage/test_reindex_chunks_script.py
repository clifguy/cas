"""Tests for the reindex-chunks-with-heading-context batch script.

Exercises the script's core logic (``reindex_with_services``) with stub
fixtures rather than the full production initialization path. Verifies:

- Documents whose ``adapter_version`` is below the current adapter
  ``VERSION`` are re-indexed.
- Re-indexing replaces chunk embeddings using ``heading_path + content``
  as embedder input (so semantic search reaches heading-only queries).
- Chunk ``content`` and ``heading_path`` fields are preserved unchanged.
- ``adapter_version`` on the document record is bumped after re-index.
- Documents already at current ``VERSION`` are skipped (idempotency).
- Dry-run mode does not mutate state.
"""

import hashlib
import re

from sage.adapters.interfaces import Chunk
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document
from sage.source_adapters.docx_adapter import DocxAdapter
from scripts.reindex_chunks_with_heading_context import reindex_with_services

_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "doc_old"; this helper wraps them so the values still
    construct valid Document instances. Idempotent: an already-canonical
    id passes through unchanged so wrapping is safe to apply at every
    call site.
    """
    if _DOC_ID_RE.fullmatch(name):
        return name
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


class _RecordingEmbedder:
    """Captures the texts passed to embed() and returns deterministic vectors."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        # Deterministic per-text vector; first dim encodes hash to detect changes.
        return [[float(hash(t) % 10000) / 10000.0] + [0.0] * 767 for t in texts]


def _make_doc(
    *,
    doc_id: str,
    source_type: str = SourceType.DOCX.value,
    adapter_version: str = "0.1.0",
) -> Document:
    """Construct a minimal Document record for testing."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    return Document(
        id=doc_id,
        title=f"doc {doc_id}",
        source_path=f"imports/{doc_id}.docx",
        source_type=source_type,
        source_content_hash="sha256:test",
        adapter_version=adapter_version,
        created_by="test",
        last_modified_by="test",
        lifecycle_status="active",
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE.value,
        created_at=now,
        updated_at=now,
    )


async def test_reindex_replaces_embeddings_with_heading_context_combined_text(
    graph_store, stub_content_store
):
    """The embedder is called with ``heading_path + content``; chunks are
    rewritten with new embeddings; adapter_version is bumped."""
    # Set up an old-regime document with chunks indexed by content alone.
    doc = _make_doc(doc_id=_id("doc_old"), adapter_version="0.1.0")
    await graph_store.insert_document(doc)

    body = "Cryptographic accumulator seals govern each commit."
    heading = "Technical Description > Exemplary Marker"
    old_embedding = [0.1] * 768  # placeholder old-regime vector
    chunk = Chunk(
        document_id=doc.id,
        heading_path=heading,
        content=body,
        embedding=old_embedding,
        chunk_index=0,
    )
    await stub_content_store.index_chunks(doc.id, [chunk])

    embedder = _RecordingEmbedder()
    rc = await reindex_with_services(
        graph=graph_store,
        store=stub_content_store,
        embedder=embedder,
        execute=True,
        batch_size=64,
    )
    assert rc == 0

    # Embedder received heading_path + content (not content alone).
    assert len(embedder.calls) == 1
    assert len(embedder.calls[0]) == 1
    embed_input = embedder.calls[0][0]
    assert heading in embed_input
    assert body in embed_input
    assert embed_input.startswith(heading)

    # Chunk content/heading_path preserved; embedding refreshed.
    chunks_after = await stub_content_store.get_all_chunks(doc.id)
    assert len(chunks_after) == 1
    assert chunks_after[0].content == body
    assert chunks_after[0].heading_path == heading
    assert chunks_after[0].embedding != old_embedding

    # adapter_version bumped to current DocxAdapter.VERSION.
    doc_after = await graph_store.get_document(doc.id)
    assert doc_after.adapter_version == DocxAdapter.VERSION


async def test_reindex_skips_documents_already_at_current_version(graph_store, stub_content_store):
    """Documents whose adapter_version equals the current VERSION are
    skipped — no embedder calls, no chunk rewrites."""
    doc = _make_doc(doc_id=_id("doc_current"), adapter_version=DocxAdapter.VERSION)
    await graph_store.insert_document(doc)

    chunk = Chunk(
        document_id=doc.id,
        heading_path="H",
        content="body",
        embedding=[0.5] * 768,
        chunk_index=0,
    )
    await stub_content_store.index_chunks(doc.id, [chunk])

    embedder = _RecordingEmbedder()
    rc = await reindex_with_services(
        graph=graph_store,
        store=stub_content_store,
        embedder=embedder,
        execute=True,
        batch_size=64,
    )
    assert rc == 0
    assert embedder.calls == [], "embedder must not be called for up-to-date docs"


async def test_reindex_dry_run_does_not_mutate(graph_store, stub_content_store):
    """Dry run mode prints the plan but writes nothing."""
    doc = _make_doc(doc_id=_id("doc_dry"), adapter_version="0.1.0")
    await graph_store.insert_document(doc)

    chunk = Chunk(
        document_id=doc.id,
        heading_path="H",
        content="body",
        embedding=[0.7] * 768,
        chunk_index=0,
    )
    await stub_content_store.index_chunks(doc.id, [chunk])

    embedder = _RecordingEmbedder()
    rc = await reindex_with_services(
        graph=graph_store,
        store=stub_content_store,
        embedder=embedder,
        execute=False,  # dry run
        batch_size=64,
    )
    assert rc == 0
    assert embedder.calls == []

    doc_after = await graph_store.get_document(doc.id)
    assert doc_after.adapter_version == "0.1.0"  # unchanged

    chunks_after = await stub_content_store.get_all_chunks(doc.id)
    assert chunks_after[0].embedding == [0.7] * 768  # unchanged


async def test_reindex_skips_documents_with_no_chunks(graph_store, stub_content_store):
    """A document with no stored chunks is silently skipped (no version bump,
    no embedder call). This avoids redundant work for projection-only docs."""
    doc = _make_doc(doc_id=_id("doc_empty"), adapter_version="0.1.0")
    await graph_store.insert_document(doc)
    # Intentionally do not call index_chunks; doc has no chunks.

    embedder = _RecordingEmbedder()
    rc = await reindex_with_services(
        graph=graph_store,
        store=stub_content_store,
        embedder=embedder,
        execute=True,
        batch_size=64,
    )
    assert rc == 0
    assert embedder.calls == []

    doc_after = await graph_store.get_document(doc.id)
    # Version unchanged because no chunks were indexed; future re-ingest can fix.
    assert doc_after.adapter_version == "0.1.0"


async def test_reindex_preserves_chunk_doc_type(graph_store, stub_content_store):
    """Re-index must preserve ``chunk.doc_type``. A read→re-embed→write
    cycle that drops doc_type wipes the column for every chunk and breaks
    ``filters={"doc_type": ...}`` queries vault-wide. Regression guard for
    the live-data incident on 2026-05-04 (PIM Health).
    """
    doc = _make_doc(doc_id=_id("doc_dtype"), adapter_version="0.1.0")
    await graph_store.insert_document(doc)

    chunk = Chunk(
        document_id=doc.id,
        heading_path="H",
        content="body content",
        embedding=[0.0] * 768,
        chunk_index=0,
        doc_type="patent_draft",
    )
    await stub_content_store.index_chunks(doc.id, [chunk])

    embedder = _RecordingEmbedder()
    rc = await reindex_with_services(
        graph=graph_store,
        store=stub_content_store,
        embedder=embedder,
        execute=True,
        batch_size=64,
    )
    assert rc == 0

    chunks_after = await stub_content_store.get_all_chunks(doc.id)
    assert len(chunks_after) == 1
    assert chunks_after[0].doc_type == "patent_draft", (
        "Re-index round-trip dropped chunk.doc_type. The content store's "
        "get_all_chunks must populate doc_type, and the script must not "
        "discard it before calling index_chunks."
    )


async def test_reindex_handles_source_type_with_no_adapter_registered(
    graph_store, stub_content_store
):
    """Documents whose source_type has no adapter VERSION registered in
    SOURCE_TYPE_TO_VERSION are skipped (e.g. forward-declared types like
    email/onenote/teams_chat)."""
    doc = _make_doc(
        doc_id=_id("doc_email"),
        source_type=SourceType.EMAIL.value,
        adapter_version="0.1.0",
    )
    await graph_store.insert_document(doc)

    chunk = Chunk(
        document_id=doc.id,
        heading_path="H",
        content="body",
        embedding=[0.0] * 768,
        chunk_index=0,
    )
    await stub_content_store.index_chunks(doc.id, [chunk])

    embedder = _RecordingEmbedder()
    rc = await reindex_with_services(
        graph=graph_store,
        store=stub_content_store,
        embedder=embedder,
        execute=True,
        batch_size=64,
    )
    assert rc == 0
    assert embedder.calls == []
