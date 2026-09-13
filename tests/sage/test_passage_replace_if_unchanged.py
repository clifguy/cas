"""Replacing a document's passages only if they are still what was read.

A caller that reads passages, works on them, and writes a replacement cannot
make the comparison and the write separate steps: a write landing between them
is overwritten. The store performs both under the document's write lock, which
every passage writer takes, so a concurrent writer either lands before the
comparison -- and the replacement is refused -- or waits until it commits.

Each refusal fixture changes exactly one axis the replacement would overwrite --
content, heading path, or a column a metadata writer stamps -- and keeps the row
count, so a comparison omitting that axis passes its test and fails no other.
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


def _rows(label: str, paths=PATHS, lifecycle_status: str = "active") -> list[Chunk]:
    return [
        Chunk(
            document_id=DOC_ID,
            heading_path=path,
            content=f"{label} passage {index}",
            embedding=[0.1] * EMBEDDING_DIM,
            chunk_index=index,
            section_index=min(index, 1),
            doc_type="misc",
            lifecycle_status=lifecycle_status,
            project="CAS",
            indexed_structure=path.removeprefix("Doc > ").removeprefix("Doc"),
        )
        for index, path in enumerate(paths)
    ]


def _state(chunks: list[Chunk]) -> list:
    return [c.stored_state for c in chunks]


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
        DOC_ID, _state(_rows("original")), _rows("divided")
    )

    assert replaced is True
    assert _read(await store.get_all_chunks(DOC_ID)) == _read(_rows("divided"))


async def test_refuses_when_content_changed_under_the_same_shape(store) -> None:
    await store.index_chunks(DOC_ID, _rows("re-indexed"))

    replaced = await store.replace_chunks_if_unchanged(
        DOC_ID, _state(_rows("original")), _rows("divided")
    )

    assert replaced is False
    assert _read(await store.get_all_chunks(DOC_ID)) == _read(_rows("re-indexed"))


async def test_refuses_when_heading_paths_changed_under_the_same_content(store) -> None:
    """A parent heading renamed on re-ingest moves every descendant's path while
    leaving each passage's content -- which carries only its own heading line --
    byte-identical."""
    renamed = ("Renamed", "Renamed > Long", "Renamed > Long")
    await store.index_chunks(DOC_ID, _rows("original", paths=renamed))

    replaced = await store.replace_chunks_if_unchanged(
        DOC_ID, _state(_rows("original")), _rows("divided")
    )

    assert replaced is False
    assert [c.heading_path for c in await store.get_all_chunks(DOC_ID)] == list(renamed)


async def test_refuses_when_a_metadata_writer_stamped_the_passages(store) -> None:
    """The replacement carries the stamped columns from the rows it read, so a
    stamp landing after the read would otherwise be written back."""
    await store.index_chunks(DOC_ID, _rows("original"))
    expected = _state(_rows("original"))
    await store.update_chunk_metadata(DOC_ID, {"lifecycle_status": "archived"})

    replaced = await store.replace_chunks_if_unchanged(DOC_ID, expected, _rows("divided"))

    assert replaced is False
    assert {c.lifecycle_status for c in await store.get_all_chunks(DOC_ID)} == {"archived"}


async def test_a_replacement_waits_for_a_writer_holding_the_document(pg_pool) -> None:
    store = PostgresContentStore(pg_pool)
    await store.index_chunks(DOC_ID, _rows("original"))

    async with pg_pool.connection() as conn, conn.transaction():
        await conn.execute(DOCUMENT_PASSAGE_WRITE_LOCK_SQL, (DOC_ID,))
        task = asyncio.create_task(
            store.replace_chunks_if_unchanged(DOC_ID, _state(_rows("original")), _rows("divided"))
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


@pytest.mark.parametrize(
    "writer",
    ["index_chunks", "remove_document", "update_chunk_metadata", "update_indexed_structure"],
)
async def test_every_passage_writer_takes_the_document_write_lock(pg_pool, writer) -> None:
    store = PostgresContentStore(pg_pool)
    await store.index_chunks(DOC_ID, _rows("original"))
    call = {
        "index_chunks": lambda: store.index_chunks(DOC_ID, _rows("re-indexed")),
        "remove_document": lambda: store.remove_document(DOC_ID),
        "update_chunk_metadata": lambda: store.update_chunk_metadata(
            DOC_ID, {"lifecycle_status": "archived"}
        ),
        "update_indexed_structure": lambda: store.update_indexed_structure(
            DOC_ID, [("Doc > Long", "Renamed")]
        ),
    }[writer]()

    async with pg_pool.connection() as conn, conn.transaction():
        await conn.execute(DOCUMENT_PASSAGE_WRITE_LOCK_SQL, (DOC_ID,))
        task = asyncio.create_task(call)
        await asyncio.sleep(0.3)
        assert not task.done(), f"{writer} did not wait for the document's write lock"

    await task
