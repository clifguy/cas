"""Event-loop offload + write-serialization tests for LanceDBContentStore.

Asserts that the blocking LanceDB write path (``table.add`` + the FTS
index maintenance) runs off the asyncio event loop and that concurrent
writers to the single content table are serialized. Reads stay on the
loop: while a write is in flight off-loop, a concurrent ``search`` must
return within normal latency (no event-loop freeze).

Uses a real embedded LanceDBContentStore against a tmp dir -- fast on a
fresh directory -- and gates the write at ``_ensure_fts_indexes`` so the
test controls when the worker thread is mid-write.
"""

import asyncio
import threading
import time
from collections.abc import Callable

import pytest

from sage.adapters.content_store_lancedb import VECTOR_DIMENSIONS, LanceDBContentStore
from sage.adapters.interfaces import Chunk


def _chunk(document_id: str, *, content: str = "body text", lead: float = 0.0) -> Chunk:
    """Build a chunk with a deterministic unit-ish embedding.

    ``lead`` sets the first vector component so a query embedding can be
    aimed at a specific document under cosine distance.
    """
    embedding = [lead] + [0.0] * (VECTOR_DIMENSIONS - 1)
    return Chunk(
        document_id=document_id,
        heading_path="Section",
        content=content,
        embedding=embedding,
        chunk_index=0,
    )


@pytest.fixture
def store(tmp_path):
    return LanceDBContentStore(tmp_path)


async def _wait_until(predicate: Callable[[], bool]) -> None:
    while not predicate():
        await asyncio.sleep(0.005)


async def test_index_chunks_write_off_loop_search_stays_responsive(store, monkeypatch):
    """While a write is gated mid-FTS-index-maintenance in its worker
    thread, a concurrent ``search_semantic`` returns within normal latency.

    Regression guard for the on-loop block: if the FTS index maintenance
    ran on the loop, the gated write would freeze it and both the
    ``_wait_until`` probe and the concurrent search would time out.
    """
    # A fully-indexed document the concurrent read will retrieve.
    await store.index_chunks("aaaaaaaa_existing", [_chunk("aaaaaaaa_existing", lead=1.0)])

    started = threading.Event()
    release = threading.Event()
    in_progress = threading.Event()

    def blocking_ensure(table):
        in_progress.set()
        started.set()
        release.wait(timeout=2.0)
        in_progress.clear()

    monkeypatch.setattr(store, "_ensure_fts_indexes", blocking_ensure)

    write_task = asyncio.create_task(
        store.index_chunks("bbbbbbbb_new", [_chunk("bbbbbbbb_new", lead=0.5)])
    )
    try:
        await asyncio.wait_for(_wait_until(started.is_set), timeout=1.0)
        assert in_progress.is_set()

        query = [1.0] + [0.0] * (VECTOR_DIMENSIONS - 1)
        results = await asyncio.wait_for(store.search_semantic(query, limit=5), timeout=1.0)

        assert in_progress.is_set(), "write completed before the read; no concurrency shown"
        assert any(r.document_id == "aaaaaaaa_existing" for r in results)
    finally:
        release.set()

    await asyncio.wait_for(write_task, timeout=2.0)


async def test_concurrent_writes_serialize_to_one_table(store, monkeypatch):
    """Two concurrent ``index_chunks`` calls never overlap in the write
    body: at most one writer is active at any instant.

    Pins the serialization requirement. Without the per-instance write
    lock, both coroutines would run their write bodies on separate
    default-executor threads and ``max_active`` would reach 2.
    """
    counter_lock = threading.Lock()
    active = 0
    max_active = 0
    real_ensure = store._ensure_fts_indexes

    def counting_ensure(table):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)  # widen the overlap window
        with counter_lock:
            active -= 1
        real_ensure(table)

    monkeypatch.setattr(store, "_ensure_fts_indexes", counting_ensure)

    await asyncio.gather(
        store.index_chunks("aaaaaaaa_doc_a", [_chunk("aaaaaaaa_doc_a")]),
        store.index_chunks("bbbbbbbb_doc_b", [_chunk("bbbbbbbb_doc_b")]),
    )

    assert max_active == 1, f"writers overlapped (max_active={max_active}); not serialized"
    assert await store.has_chunks("aaaaaaaa_doc_a")
    assert await store.has_chunks("bbbbbbbb_doc_b")
