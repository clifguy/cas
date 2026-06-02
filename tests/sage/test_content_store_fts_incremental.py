"""Incremental-FTS tests for LanceDBContentStore.

Pins the contract that the FTS indexes are built **once** and maintained
incrementally rather than fully rebuilt after every mutation. New rows are
covered by LanceDB's native delta scan and deletes are honored immediately,
so search stays consistent with the underlying rows without a per-write
``create_fts_index`` rebuild. Positions are not stored (no phrase queries
are ever issued), so the index build carries no positional dead weight.

Uses a real embedded LanceDBContentStore against a tmp dir -- fast on a
fresh directory.
"""

import lancedb.table
import pytest

from sage.adapters.content_store_lancedb import VECTOR_DIMENSIONS, LanceDBContentStore
from sage.adapters.interfaces import Chunk


def _chunk(
    document_id: str,
    *,
    content: str,
    heading_path: str = "Section",
    chunk_index: int = 0,
) -> Chunk:
    """Build a chunk with a zero embedding (BM25 tests ignore the vector)."""
    return Chunk(
        document_id=document_id,
        heading_path=heading_path,
        content=content,
        embedding=[0.0] * VECTOR_DIMENSIONS,
        chunk_index=chunk_index,
    )


@pytest.fixture
def store(tmp_path):
    return LanceDBContentStore(tmp_path)


async def test_fts_index_built_once_not_rebuilt_per_write(store, monkeypatch):
    """Across five sequential single-document writes, ``create_fts_index``
    is called exactly twice -- once for ``content``, once for
    ``heading_path`` -- and never with positional indexing.

    Guards the core change: the per-write full rebuild is gone (the old
    behavior would call ``create_fts_index`` 2x per write = 10 times), the
    bloat root cause (a full-table index version minted per write) is
    removed, and ``with_position`` dead weight is dropped.
    """
    calls: list[tuple[tuple, dict]] = []
    original = lancedb.table.LanceTable.create_fts_index

    def spy(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(lancedb.table.LanceTable, "create_fts_index", spy)

    for i in range(5):
        await store.index_chunks(
            f"doc_{i:08d}",
            [_chunk(f"doc_{i:08d}", content=f"alpha bravo charlie token{i}")],
        )

    assert len(calls) == 2, (
        f"expected the index to be built once per column (2 calls total), "
        f"got {len(calls)} -- a per-write rebuild is still present"
    )
    indexed_columns = {args[0] for args, _ in calls}
    assert indexed_columns == {"content", "heading_path"}
    for _, kwargs in calls:
        assert not kwargs.get("with_position"), "positions are dead weight (no phrase queries)"
        assert not kwargs.get("use_tantivy"), "must use the native FTS backend (incremental)"


async def test_search_consistent_across_writes_without_rebuild(store):
    """Sequential multi-document writes stay searchable, deletes are
    honored, and a heading-only term is findable -- all without any
    explicit index rebuild or ``optimize`` call.

    Complements the single-doc round-trip in test_adapters::test_ad_019 by
    exercising the incremental delta across more than one document and a
    heading-only match (the ``heading_path`` index).
    """
    await store.index_chunks("doc_aaaa", [_chunk("doc_aaaa", content="alphaword shared body")])
    res = await store.search_bm25("alphaword", limit=10)
    assert any(r.document_id == "doc_aaaa" for r in res)

    # A second document, plus a chunk whose distinctive term lives ONLY in
    # the heading_path (not the body).
    await store.index_chunks(
        "doc_bbbb",
        [
            _chunk("doc_bbbb", content="betaword shared body", chunk_index=0),
            _chunk(
                "doc_bbbb",
                content="ordinary body with nothing special",
                heading_path="Topic > gammaword overview",
                chunk_index=1,
            ),
        ],
    )

    # All three terms findable immediately, no optimize() anywhere.
    assert any(r.document_id == "doc_aaaa" for r in await store.search_bm25("alphaword", limit=10))
    assert any(r.document_id == "doc_bbbb" for r in await store.search_bm25("betaword", limit=10))
    assert any(
        r.document_id == "doc_bbbb" for r in await store.search_bm25("gammaword", limit=10)
    ), "heading-only term must surface via the heading_path FTS index"

    # Delete A: its term disappears immediately; B's persists.
    await store.remove_document("doc_aaaa")
    assert await store.search_bm25("alphaword", limit=10) == []
    assert any(r.document_id == "doc_bbbb" for r in await store.search_bm25("betaword", limit=10))
