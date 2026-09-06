"""Ingest and the migration derive one structure, not two (CAS-ADR-049 §3).

The decision's consequence list is explicit that the passage's relative
structure has two writers -- an ingest-side one and a migration-side one -- and
that they must apply one rule, "or a re-ingested document and a migrated one
carry different structure for the same source". That is what this module pins,
by driving both writers over the same source and comparing what they stored.

The structural pin sits alongside the behavioural one: an equality assertion
over one fixture passes for two independent implementations that happen to agree
on it. Asserting that both writers import the same function is what says they
cannot drift on a fixture nobody wrote.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import Chunk
from sage.adapters.stubs import StubAbstractionProvider, StubEmbeddingProvider
from sage.models.enums import SourceType
from sage.models.schemas import Document, IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.maintenance import MaintenanceService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.storage.locks import DocumentLockManager
from sage.storage.postgres.schema import EMBEDDING_DIM, TEXT_SEARCH_CONFIG

pytestmark = pytest.mark.asyncio

_TITLE = "T-0002: Zzagreeword across the retrieval surface"

_SOURCE = f"""# {_TITLE}

Opening prose under the title.

## Problem

Prose under the problem heading.

### A nested case

Prose one level deeper.

## Design notes

Prose under the design notes.
"""

_PRE_CHANGE_TSV = (
    "ALTER TABLE chunks DROP COLUMN IF EXISTS tsv;"
    " ALTER TABLE chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS ("
    f"setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', heading_path), 'A')"
    f" || setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', content), 'D')"
    ") STORED;"
    " CREATE INDEX IF NOT EXISTS idx_chunks_tsv_gin ON chunks USING GIN (tsv);"
)


@pytest.fixture
async def store(pg_pool):
    return PostgresContentStore(pg_pool)


@pytest.fixture
def ingesting(store, postgres_graph_store, minimal_config, tmp_vault_dir):
    """The real ingestion pipeline over the Postgres passage surface.

    The shared ``ingestion_service`` fixture binds the in-memory double, which
    has no generated vector and no stored column -- so it could not be evidence
    about what a vault actually holds.
    """
    return IngestionService(
        graph_store=postgres_graph_store,
        lock_manager=DocumentLockManager(),
        content_store=store,
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
        config=minimal_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )


def _doc(document_id: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=document_id,
        title=_TITLE,
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
    )


async def _ingest(ingesting, tmp_vault_dir, name: str):
    """Ingest the shared source through the real pipeline, synchronously."""
    source_dir = tmp_vault_dir / "sources" / "test"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / name).write_text(_SOURCE)
    return await ingesting.ingest(
        IngestRequest(source=f"test/{name}", source_type=SourceType.MARKDOWN)
    )


async def _structure_by_address(pg_pool, document_id: str) -> dict[str, str | None]:
    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT heading_path, indexed_structure FROM chunks WHERE document_id = %s",
            (document_id,),
        )
        return {r[0]: r[1] for r in await cur.fetchall()}


async def test_a_migrated_document_carries_what_a_freshly_ingested_one_does(
    ingesting, store, postgres_graph_store, minimal_config, tmp_vault_dir, pg_pool
):
    """The two writers agree on the same source, address for address.

    Path A ingests the document through the real pipeline, which derives the
    structure as it writes each passage. Path B puts the same passages in the
    pre-decision layout -- underived, old vector -- and runs the migration over
    them. The stored structure must be identical.

    Anti-coincidental-pass, and this is the whole difficulty of the test: if
    both writers did nothing, or both wrote the address verbatim, or both wrote
    NULL, the two maps would still be equal and this would assert nothing. Three
    controls make the equality mean something -- at least one address must
    differ from its structure, at least one structure must be the empty string
    the H1's own passage carries, and none may be NULL.
    """
    result = await _ingest(ingesting, tmp_vault_dir, "agreement.md")
    ingest_side = await _structure_by_address(pg_pool, result.document.id)

    assert ingest_side, "control: ingest wrote passages to compare against"
    assert None not in ingest_side.values(), (
        "ingest must derive every passage; an underived one would fall back to "
        "its address and match a do-nothing migration for the wrong reason"
    )
    assert any(structure != address for address, structure in ingest_side.items()), (
        "control: at least one passage must differ from its address, or two "
        "writers that both copied the path would satisfy this test"
    )
    assert "" in ingest_side.values(), (
        "control: the H1's own passage carries an empty structure, which is the "
        "value a writer confusing 'underived' with 'empty' gets wrong"
    )

    # Path B: the same passages as a pre-decision vault holds them.
    migrated_id = "00000002_migrated"
    await postgres_graph_store.insert_document(_doc(migrated_id))
    await store.index_chunks(
        migrated_id,
        [
            Chunk(
                document_id=migrated_id,
                heading_path=address,
                content=f"body under {address}",
                embedding=[0.0] * EMBEDDING_DIM,
                chunk_index=index,
            )
            for index, address in enumerate(ingest_side)
        ],
    )
    async with pg_pool.connection() as conn:
        await conn.execute(
            "UPDATE chunks SET indexed_structure = NULL WHERE document_id = %s", (migrated_id,)
        )
        await conn.execute(_PRE_CHANGE_TSV)

    await MaintenanceService(
        vault_id=minimal_config.vault.id,
        graph_store=postgres_graph_store,
        config=minimal_config,
        registry_service=None,
        content_store=store,
        vault_dir=Path(tmp_vault_dir),
    ).migrate_vault()

    assert await _structure_by_address(pg_pool, migrated_id) == ingest_side


async def test_ingest_leaves_no_passage_underived(ingesting, pg_pool, tmp_vault_dir):
    """A caller forgetting to derive the structure fails loudly here.

    Anti-coincidental-pass for the design itself: the coalesce fallback makes
    an underived passage behave exactly as it did before the decision, so a
    writer that silently stopped deriving would degrade rather than break, and
    no ranking assertion would notice. Only a count of NULLs after a real
    ingest catches it.
    """
    result = await _ingest(ingesting, tmp_vault_dir, "underived.md")

    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FILTER (WHERE indexed_structure IS NULL), count(*) "
            "FROM chunks WHERE document_id = %s",
            (result.document.id,),
        )
        underived, total = await cur.fetchone()
    assert total, "control: the ingest wrote passages"
    assert underived == 0


async def test_ingest_leaves_the_address_rooted_at_the_title(ingesting, pg_pool, tmp_vault_dir):
    """Both halves of Decision 3, asserted on the same passage.

    The address keeps the title root; the indexed structure drops it. Stated
    together rather than in two modules because the failure this guards against
    -- rewriting the stored path instead of adding a field -- would otherwise
    surface only in a distant round-trip test, where it reads as a projection
    defect rather than as the decision being misread.
    """
    result = await _ingest(ingesting, tmp_vault_dir, "address.md")

    stored = await _structure_by_address(pg_pool, result.document.id)
    problem_address = f"{_TITLE} > Problem"
    assert problem_address in stored, "the address keeps the title its source made its H1"
    assert stored[problem_address] == "Problem", "the indexed structure does not"
