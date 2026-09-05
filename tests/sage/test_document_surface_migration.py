"""Relocating document-level text off the passage surface (CAS-ADR-049).

A vault provisioned before document-level text had a surface of its own holds a
synthetic header row per document among its passages. The migration moves that
text to the new surface, and must be safe to run again on a vault it has
already repaired.

Stored heading paths are deliberately not rewritten here. CAS-ADR-049 also
places a passage's structure relative to its document, but for a source whose
title is also its top-level heading the two clauses of that decision name the
same string, and rewriting the paths changes how every section of such a
document is addressed. That half is tracked separately.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import LEGACY_DOCUMENT_HEADER_HEADING_PATH, Chunk
from sage.models.enums import SourceType
from sage.models.schemas import Document
from sage.services.maintenance import (
    BACKFILL_DOCUMENT_SURFACE,
    MaintenanceService,
)
from sage.storage.postgres.schema import EMBEDDING_DIM

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def store(pg_pool):
    return PostgresContentStore(pg_pool)


def _doc(document_id: str, title: str, source_type=SourceType.MARKDOWN) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=document_id,
        title=title,
        source_type=source_type,
        source_path=f"imports/{document_id}.md",
        lifecycle_status="active",
        source_content_hash=f"sha256:{0:064x}",
        adapter_version="1",
        created_by="t",
        created_at=now,
        last_modified_by="t",
        updated_at=now,
        doc_type="adr",
        tags=["retrieval"],
        semantic_abstract="A generated summary sentence.",
    )


def _legacy_header(document_id: str, title: str) -> Chunk:
    """A header row exactly as the pre-decision pipeline wrote one."""
    return Chunk(
        document_id=document_id,
        heading_path=LEGACY_DOCUMENT_HEADER_HEADING_PATH,
        content=(
            f"Title: {title}\n"
            f"Source: {document_id}\n"
            "Tags: retrieval\n"
            "Abstract: A generated summary sentence.\n\n"
            "Identifier tokens: legacy tokens\n"
        ),
        embedding=[0.25] * EMBEDDING_DIM,
        chunk_index=-1,
    )


async def _seed_legacy(store, graph_store, document_id, title, heading_paths):
    """Seed one document in the pre-decision layout."""
    doc = _doc(document_id, title)
    await graph_store.insert_document(doc)
    await store.index_chunks(
        document_id,
        [
            _legacy_header(document_id, title),
            *[
                Chunk(
                    document_id=document_id,
                    heading_path=path,
                    content=f"body under {path}",
                    embedding=[0.0] * EMBEDDING_DIM,
                    chunk_index=i,
                )
                for i, path in enumerate(heading_paths)
            ],
        ],
    )
    return doc


def _maintenance(graph_store, store, config, tmp_vault_dir) -> MaintenanceService:
    return MaintenanceService(
        vault_id=config.vault.id,
        graph_store=graph_store,
        config=config,
        registry_service=None,
        content_store=store,
        vault_dir=Path(tmp_vault_dir),
    )


# ---------------------------------------------------------------------------
# Relocation
# ---------------------------------------------------------------------------


async def test_migration_relocates_legacy_header_rows(
    store, postgres_graph_store, minimal_config, tmp_vault_dir
):
    """Header rows leave the passage surface and become document-level rows."""
    title = "Deltaword Catalog"
    await _seed_legacy(store, postgres_graph_store, "00000001_doc", title, ["Body"])
    assert await store.legacy_document_header_rows(), "precondition: a legacy row exists"

    report = await _maintenance(
        postgres_graph_store, store, minimal_config, tmp_vault_dir
    ).migrate_vault()

    assert BACKFILL_DOCUMENT_SURFACE in report.backfills_applied
    assert await store.legacy_document_header_rows() == [], (
        "the legacy row must be gone from the passage surface"
    )
    assert [c.heading_path for c in await store.get_all_chunks("00000001_doc")] == ["Body"], (
        "authored passages survive the relocation untouched"
    )
    # The relocated text is reachable, and matchable because it is authored.
    assert [r.document_id for r in await store.search_bm25("deltaword", limit=10)] == [
        "00000001_doc"
    ]


async def test_migration_preserves_provenance_of_relocated_text(
    store, postgres_graph_store, minimal_config, tmp_vault_dir
):
    """A migrated vault obeys the provenance rule, not just a fresh one.

    The relocated row is recomposed from the stored record rather than parsed
    out of the header's composed text, so the abstract and filename stem land
    in the derived half exactly as they would at ingest.
    """
    await _seed_legacy(store, postgres_graph_store, "00000001_doc", "Some Title", ["Body"])

    await _maintenance(postgres_graph_store, store, minimal_config, tmp_vault_dir).migrate_vault()

    assert await store.search_bm25("generated summary sentence", limit=10) == [], (
        "the abstract is derived text and must not be matchable after migration"
    )
    assert [r.document_id for r in await store.search_bm25("some title", limit=10)] == [
        "00000001_doc"
    ], "positive control: the authored title is matchable after migration"


async def test_migration_carries_the_legacy_embedding_forward(
    store, postgres_graph_store, minimal_config, tmp_vault_dir
):
    """The relocated row keeps a vector, so the corpus is not re-embedded.

    A migration that dropped the embedding would leave every document
    unreachable on the semantic arm until an operator reabstracted the vault.
    """
    await _seed_legacy(store, postgres_graph_store, "00000001_doc", "Some Title", ["Body"])

    await _maintenance(postgres_graph_store, store, minimal_config, tmp_vault_dir).migrate_vault()

    hits = await store.search_semantic([0.25] * EMBEDDING_DIM, limit=10)
    assert any(h.document_id == "00000001_doc" and h.heading_path == "" for h in hits), (
        "the relocated document-level row did not reach the semantic arm"
    )


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


async def test_migration_is_idempotent(store, postgres_graph_store, minimal_config, tmp_vault_dir):
    """A second run repairs nothing and changes nothing.

    Both halves are self-detecting: the pass is driven by the presence of
    legacy rows, and a heading path already relative to its document is no
    longer rooted at the title.
    """
    title = "Deltaword Catalog"
    await _seed_legacy(store, postgres_graph_store, "00000001_doc", title, [f"{title} > Overview"])
    service = _maintenance(postgres_graph_store, store, minimal_config, tmp_vault_dir)

    first = await service.migrate_vault()
    assert BACKFILL_DOCUMENT_SURFACE in first.backfills_applied, (
        "control: the first run must actually repair something, or the second "
        "run's silence proves nothing"
    )
    after_first = [c.heading_path for c in await store.get_all_chunks("00000001_doc")]

    second = await service.migrate_vault()

    assert BACKFILL_DOCUMENT_SURFACE not in second.backfills_applied
    assert [c.heading_path for c in await store.get_all_chunks("00000001_doc")] == after_first
    assert [r.document_id for r in await store.search_bm25("deltaword", limit=10)] == [
        "00000001_doc"
    ], "the relocated row survives a second run rather than being duplicated or dropped"


async def test_migration_reports_nothing_on_a_clean_vault(
    store, postgres_graph_store, minimal_config, tmp_vault_dir
):
    """A vault with no legacy state names neither backfill."""
    doc = _doc("00000001_doc", "Some Title")
    await postgres_graph_store.insert_document(doc)
    await store.index_chunks(
        "00000001_doc",
        [
            Chunk(
                document_id="00000001_doc",
                heading_path="Body",
                content="body",
                embedding=[0.0] * EMBEDDING_DIM,
                chunk_index=0,
            )
        ],
    )

    report = await _maintenance(
        postgres_graph_store, store, minimal_config, tmp_vault_dir
    ).migrate_vault()

    assert BACKFILL_DOCUMENT_SURFACE not in report.backfills_applied


async def test_migration_reclaims_a_header_row_whose_document_is_gone(
    store, postgres_graph_store, minimal_config, tmp_vault_dir
):
    """An orphaned header row is removed rather than stalling the pass.

    There is nothing to compose a document-level row from, and leaving the row
    in place would keep derived text on the passage surface indefinitely.
    """
    await store.index_chunks(
        "00000099_orphan",
        [_legacy_header("00000099_orphan", "Orphaned Title")],
    )

    await _maintenance(postgres_graph_store, store, minimal_config, tmp_vault_dir).migrate_vault()

    assert await store.legacy_document_header_rows() == []
