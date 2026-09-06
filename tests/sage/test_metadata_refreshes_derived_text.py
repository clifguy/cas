"""A metadata edit re-derives what the store holds *about* a document.

A document's title and tags are authored text, and CAS-ADR-049 has the binding
index them in two derived places: the document surface's matchable half, and --
because a passage's indexed structure is its heading path relative to the
document -- every passage of that document. Both are computed from the record at
ingest, so an edit to the record leaves both describing a title nobody holds any
more, and the document goes on matching its old title rather than its new one.

Against a real backend throughout: the assertions turn on what satisfies a match
across two surfaces, which the in-memory double models neither of.
"""

from datetime import datetime, timezone

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import Chunk
from sage.models.enums import SourceType
from sage.models.schemas import Document, ListFieldPatch, UpdateMetadataRequest
from sage.services.document_surface import compose_document_surface
from sage.services.metadata import MetadataService
from sage.services.passage_structure import indexed_structure
from sage.storage.locks import DocumentLockManager
from sage.storage.postgres.schema import EMBEDDING_DIM

pytestmark = pytest.mark.asyncio

_OLD_TITLE = "T-0001: Zzoldword and the retrieval surface"
_NEW_TITLE = "T-0001: Zznewword and the retrieval surface"


@pytest.fixture
async def store(pg_pool):
    return PostgresContentStore(pg_pool)


@pytest.fixture
def metadata(postgres_graph_store, store, minimal_config):
    return MetadataService(
        graph_store=postgres_graph_store,
        lock_manager=DocumentLockManager(),
        config=minimal_config,
        content_store=store,
    )


def _doc(document_id: str, title: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=document_id,
        title=title,
        source_type=SourceType.MARKDOWN,
        source_path=f"imports/{document_id}.md",
        lifecycle_status="active",
        source_content_hash=f"sha256:{0:064x}",
        adapter_version="1",
        created_by="t",
        created_at=now,
        last_modified_by="t",
        updated_at=now,
        doc_type="ticket",
        tags=["zzoldtag"],
    )


@pytest.fixture
async def seeded(store, postgres_graph_store):
    """One document as ingest leaves it: surface composed, passages derived."""
    document_id = "00000001_edit"
    doc = _doc(document_id, _OLD_TITLE)
    await postgres_graph_store.insert_document(doc)

    addresses = (_OLD_TITLE, f"{_OLD_TITLE} > Problem")
    await store.index_chunks(
        document_id,
        [
            Chunk(
                document_id=document_id,
                heading_path=address,
                indexed_structure=indexed_structure(address, _OLD_TITLE),
                content=f"body prose {index}",
                embedding=[0.0] * EMBEDDING_DIM,
                chunk_index=index,
            )
            for index, address in enumerate(addresses)
        ],
    )
    surface = compose_document_surface(document_id, doc)
    surface.embedding = [0.5] + [0.0] * (EMBEDDING_DIM - 1)
    await store.upsert_document_surface(surface)

    assert [r.document_id for r in await store.search_bm25("zzoldword", limit=10)] == [
        document_id
    ], "precondition: the document matches its title before the edit"
    return document_id


async def test_a_retitled_document_matches_its_new_title(metadata, store, seeded, pg_pool):
    """The defect: an edited title never reached the surface that carries it.

    Before this fix the document surface went on carrying the title the record
    held at ingest, so a retitled document was reachable only by a name nobody
    could see any more.

    The old title does *not* stop matching, and that is correct rather than a
    leak: the document's own H1 still reads it, and a heading someone wrote is
    authored text whatever the record is subsequently called. Retitling a record
    does not edit the document. Asserted here so the distinction is deliberate
    -- an implementation that silenced the old term would be rewriting content
    to satisfy a metadata edit.
    """
    await metadata._update_metadata(seeded, UpdateMetadataRequest(title=_NEW_TITLE), "t")

    assert [r.document_id for r in await store.search_bm25("zznewword", limit=10)] == [seeded], (
        "the document must match the title it now carries"
    )
    assert [r.document_id for r in await store.search_bm25("zzoldword", limit=10)] == [seeded], (
        "and goes on matching its own authored heading, which the edit did not touch"
    )

    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT embedding IS NOT NULL FROM document_surface WHERE document_id = %s",
            (seeded,),
        )
        vector_survived = (await cur.fetchone())[0]
    assert vector_survived, (
        "the row's vector must survive the text rewrite. This service has no "
        "embedding provider, so an implementation that replaced the row wholesale "
        "would null it -- and every keyword assertion above would still pass, "
        "because the keyword arm never reads a vector"
    )


async def test_a_retag_reaches_the_surface(metadata, store, seeded):
    """Tags are authored text too, and reach matching through the same row."""
    await metadata._update_metadata(
        seeded,
        UpdateMetadataRequest(tags=ListFieldPatch(add=["zznewtag"], remove=["zzoldtag"])),
        "t",
    )

    assert [r.document_id for r in await store.search_bm25("zznewtag", limit=10)] == [seeded]
    assert await store.search_bm25("zzoldtag", limit=10) == []


async def test_a_retitle_re_derives_every_passage_structure(metadata, store, seeded, pg_pool):
    """The half this change introduced: structure is derived from the title.

    A passage's indexed structure strips a root element equal to the document
    title, so after a retitle the stored value was computed against a title the
    record no longer holds. Left alone, the *old* title stays stripped and the
    *new* one starts being indexed into every passage -- the exact condition
    CAS-ADR-049 Decision 3 removes.

    Anti-coincidental-pass: the addresses are asserted unchanged in the same
    test. A fix that re-derived by rewriting the stored heading paths would
    satisfy every structural assertion here and silently break addressing,
    which is the rejected reading of the decision.
    """
    await metadata._update_metadata(seeded, UpdateMetadataRequest(title=_NEW_TITLE), "t")

    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT heading_path, indexed_structure FROM chunks WHERE document_id = %s"
            " ORDER BY chunk_index",
            (seeded,),
        )
        rows = await cur.fetchall()

    assert [address for address, _ in rows] == [_OLD_TITLE, f"{_OLD_TITLE} > Problem"], (
        "the address is what the source produced and a retitle does not move it"
    )
    assert [structure for _, structure in rows] == [_OLD_TITLE, f"{_OLD_TITLE} > Problem"], (
        "with the title changed, the old root is no longer the title, so nothing "
        "is stripped and the whole address is what gets indexed"
    )


async def test_an_edit_that_touches_neither_leaves_the_derived_text_alone(
    metadata, seeded, pg_pool, postgres_graph_store
):
    """Only title and tags reach this text; other edits do not trigger a refresh.

    Anti-coincidental-pass, and the reason this test is shaped oddly: comparing
    the surface text before and after an unrelated edit proves nothing on its
    own, because a refresh that *did* fire would recompose the identical string
    from the unchanged record. The two implementations are indistinguishable by
    that comparison.

    So the record is desynchronized first, out of band: the graph store gets a
    new title without the content store being told. A refresh firing on a
    project edit would now pick that title up and rewrite the surface, which is
    observable. The surface holding its original text is what says no refresh
    ran.
    """
    await postgres_graph_store.update_document(seeded, {"title": _NEW_TITLE})

    await metadata._update_metadata(seeded, UpdateMetadataRequest(project="Other"), "t")

    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT matchable FROM document_surface WHERE document_id = %s", (seeded,)
        )
        matchable = (await cur.fetchone())[0]

    assert "Zzoldword" in matchable, (
        "a project edit must not refresh the derived text, so the surface still "
        "carries the title the content store was last told about"
    )
    assert "Zznewword" not in matchable
