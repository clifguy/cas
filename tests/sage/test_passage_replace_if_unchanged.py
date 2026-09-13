"""Replacing a document's passages only if they are still what was read.

A caller that reads passages, works on them, and writes a replacement cannot
make the comparison and the write separate steps: a write landing between them
is overwritten. The store performs both under the document's write lock, which
every passage writer takes, so a concurrent writer either lands before the
comparison -- and the replacement is refused -- or waits until it commits.

The fixtures change content while keeping the row count and heading paths, so a
comparison of either alone passes nothing here.
"""

from __future__ import annotations

import asyncio

import pytest

from sage.adapters.content_store_postgres import (
    DOCUMENT_PASSAGE_WRITE_LOCK_SQL,
    PostgresContentStore,
)
from sage.adapters.interfaces import Chunk
from sage.adapters.stubs import StubContentStore
from sage.storage.postgres.schema import EMBEDDING_DIM

pytestmark = pytest.mark.asyncio

DOC_ID = "0000cafe_replace_if_unchanged"
PATHS = ("Doc", "Doc > Long", "Doc > Long")


def _rows(label: str) -> list[Chunk]:
    return [
        Chunk(
            document_id=DOC_ID,
            heading_path=path,
            content=f"{label} passage {index}",
            embedding=[0.1] * EMBEDDING_DIM,
            chunk_index=index,
            section_index=min(index, 1),
        )
        for index, path in enumerate(PATHS)
    ]


def _read(chunks: list[Chunk]) -> list[tuple[str, str]]:
    return [(c.heading_path, c.content) for c in chunks]


@pytest.fixture(params=["stub", "postgres"])
def store(request):
    if request.param == "stub":
        return StubContentStore()
    return PostgresContentStore(request.getfixturevalue("pg_pool"))


async def test_replaces_passages_that_are_unchanged(store) -> None:
    await store.index_chunks(DOC_ID, _rows("original"))

    replaced = await store.replace_chunks_if_unchanged(
        DOC_ID, _read(_rows("original")), _rows("divided")
    )

    assert replaced is True
    assert _read(await store.get_all_chunks(DOC_ID)) == _read(_rows("divided"))


async def test_refuses_when_content_changed_under_the_same_shape(store) -> None:
    await store.index_chunks(DOC_ID, _rows("re-indexed"))

    replaced = await store.replace_chunks_if_unchanged(
        DOC_ID, _read(_rows("original")), _rows("divided")
    )

    assert replaced is False
    assert _read(await store.get_all_chunks(DOC_ID)) == _read(_rows("re-indexed"))


async def test_a_replacement_waits_for_a_writer_holding_the_document(pg_pool) -> None:
    store = PostgresContentStore(pg_pool)
    await store.index_chunks(DOC_ID, _rows("original"))

    async with pg_pool.connection() as conn, conn.transaction():
        await conn.execute(DOCUMENT_PASSAGE_WRITE_LOCK_SQL, (DOC_ID,))
        task = asyncio.create_task(
            store.replace_chunks_if_unchanged(DOC_ID, _read(_rows("original")), _rows("divided"))
        )
        await asyncio.sleep(0.3)
        assert not task.done(), "the replacement did not wait for the document's writer"
        await conn.execute("DELETE FROM chunks WHERE document_id = %s", (DOC_ID,))
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO chunks (document_id, heading_path, content, chunk_index, embedding)"
                " VALUES (%s, %s, %s, %s, %s)",
                [
                    (c.document_id, c.heading_path, c.content, c.chunk_index, c.embedding)
                    for c in _rows("re-indexed")
                ],
            )

    assert await task is False
    assert _read(await store.get_all_chunks(DOC_ID)) == _read(_rows("re-indexed"))


@pytest.mark.parametrize("writer", ["index_chunks", "remove_document"])
async def test_every_passage_writer_takes_the_document_write_lock(pg_pool, writer) -> None:
    store = PostgresContentStore(pg_pool)
    await store.index_chunks(DOC_ID, _rows("original"))
    call = (
        store.index_chunks(DOC_ID, _rows("re-indexed"))
        if writer == "index_chunks"
        else store.remove_document(DOC_ID)
    )

    async with pg_pool.connection() as conn, conn.transaction():
        await conn.execute(DOCUMENT_PASSAGE_WRITE_LOCK_SQL, (DOC_ID,))
        task = asyncio.create_task(call)
        await asyncio.sleep(0.3)
        assert not task.done(), f"{writer} did not wait for the document's write lock"

    await task
