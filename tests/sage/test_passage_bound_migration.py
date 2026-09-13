"""Bringing stored passages under the embedder's input bound.

A vault indexed before sections were bounded holds passages the embedder only
partly sees. The migration re-divides them from the stored passages -- the
source is not read and nothing is re-abstracted -- and re-embeds only the
documents it rewrites.

The embedder here counts a quarter-token per byte, as real tokenizers roughly
do. That separates the cheap byte pre-check from the exact count: a passage can
exceed the pre-check and still fit, and a migration that trusted the pre-check
alone would rewrite it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import Chunk
from sage.adapters.stubs import StubAbstractionProvider, StubEmbeddingProvider
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document
from sage.services.ingestion import IngestionService
from sage.services.maintenance import BACKFILL_PASSAGE_INPUT_BOUND, MaintenanceService
from sage.services.utilities import UtilitiesService
from sage.source_adapters.base import HeadingNode, ProjectionResult
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.storage.postgres.schema import EMBEDDING_DIM

pytestmark = pytest.mark.asyncio

BOUND = 100
OVERSIZE = "00000001_oversize"
FITTING = "00000002_fitting"


class _QuarterTokenEmbedder(StubEmbeddingProvider):
    """Counts a token per four bytes and records what it embeds."""

    def __init__(self) -> None:
        super().__init__(max_input_tokens=BOUND)
        self.embedded: list[str] = []
        # Awaited inside ``embed``, where the migration is between its read of a
        # document's passages and its rewrite of them.
        self.during_embed = None

    def count_tokens(self, text: str) -> int:
        return len(text.encode("utf-8")) // 4 + 2

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        if self.during_embed is not None:
            await self.during_embed()
        return await super().embed(texts)


def _projection(document_id: str, body: str) -> ProjectionResult:
    return ProjectionResult(
        text="unused",
        headings=[
            HeadingNode(level=1, text="Doc", path="Doc", content="Opening."),
            HeadingNode(level=2, text="Long", path="Doc > Long", content=body),
            HeadingNode(level=2, text="Tail", path="Doc > Tail", content="Closing."),
        ],
        content_hash=f"sha256:{document_id}",
        adapter_version="0.1.0",
        title="Doc",
    )


def _oversize_body() -> str:
    return "\n\n".join(f"oversize paragraph {i} " + "text " * 20 for i in range(12))


def _fitting_body() -> str:
    # Over the byte pre-check (bytes + 4 > BOUND), well under the counted bound.
    return "fitting " * 30


def _doc(document_id: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=document_id,
        title="Doc",
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
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )


@pytest.fixture
async def store(pg_pool):
    return PostgresContentStore(pg_pool)


@pytest.fixture
def embedder():
    return _QuarterTokenEmbedder()


@pytest.fixture
def ingestion(graph_store, lock_manager, store, embedder, minimal_config):
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=store,
        embedding_provider=embedder,
        abstraction_provider=StubAbstractionProvider(),
        config=minimal_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )


def _legacy_rows(ingestion, document_id: str, body: str) -> list[Chunk]:
    """Passages as an unbounded chunker wrote them, before sections were numbered."""
    embedder = ingestion._embedding
    ingestion._embedding = StubEmbeddingProvider(max_input_tokens=10**9)
    try:
        chunks = ingestion._chunk_projection(document_id, _projection(document_id, body))
    finally:
        ingestion._embedding = embedder
    for chunk in chunks:
        chunk.section_index = None
        chunk.embedding = [0.25] * EMBEDDING_DIM
        chunk.doc_type = "ticket"
        chunk.lifecycle_status = "active"
        chunk.project = "CAS"
        chunk.indexed_structure = chunk.heading_path.removeprefix("Doc > ").removeprefix("Doc")
    return chunks


@pytest.fixture
async def legacy_vault(graph_store, store, ingestion, embedder):
    for document_id, body in ((OVERSIZE, _oversize_body()), (FITTING, _fitting_body())):
        await graph_store.insert_document(_doc(document_id))
        await store.index_chunks(document_id, _legacy_rows(ingestion, document_id, body))

    stored = await store.get_all_chunks(OVERSIZE)
    assert any(embedder.count_tokens(f"{c.heading_path}\n\n{c.content}") > BOUND for c in stored), (
        "the stand-in must hold a passage over the bound, or this module proves nothing"
    )
    fitting = await store.get_all_chunks(FITTING)
    assert any(len(f"{c.heading_path}\n\n{c.content}".encode()) + 2 > BOUND for c in fitting), (
        "the fitting document must trip the byte pre-check, or it cannot tell the two apart"
    )
    assert all(embedder.count_tokens(f"{c.heading_path}\n\n{c.content}") <= BOUND for c in fitting)
    embedder.embedded.clear()
    return stored


def _maintenance(graph_store, store, ingestion, config, tmp_vault_dir) -> MaintenanceService:
    return MaintenanceService(
        vault_id=config.vault.id,
        graph_store=graph_store,
        config=config,
        registry_service=None,
        content_store=store,
        ingestion_service=ingestion,
        vault_dir=Path(tmp_vault_dir),
    )


async def _projection_text(graph_store, store, config, document_id: str) -> str:
    utilities = UtilitiesService(
        graph_store=graph_store,
        content_store=store,
        embedding_provider=StubEmbeddingProvider(),
        config=config,
    )
    return (await utilities.read_projection(document_id)).projection_text


async def test_the_migration_brings_every_passage_under_the_bound(
    graph_store, store, ingestion, embedder, minimal_config, tmp_vault_dir, legacy_vault
):
    before_text = await _projection_text(graph_store, store, minimal_config, OVERSIZE)
    before_headings = await store.get_heading_paths(OVERSIZE)

    report = await _maintenance(
        graph_store, store, ingestion, minimal_config, tmp_vault_dir
    ).migrate_vault()

    assert BACKFILL_PASSAGE_INPUT_BOUND in report.backfills_applied
    after = await store.get_all_chunks(OVERSIZE)
    assert len(after) > len(legacy_vault)
    assert all(embedder.count_tokens(f"{c.heading_path}\n\n{c.content}") <= BOUND for c in after)
    assert await _projection_text(graph_store, store, minimal_config, OVERSIZE) == before_text
    assert await store.get_heading_paths(OVERSIZE) == before_headings


async def test_a_second_run_rewrites_nothing(
    graph_store, store, ingestion, embedder, minimal_config, tmp_vault_dir, legacy_vault
):
    maintenance = _maintenance(graph_store, store, ingestion, minimal_config, tmp_vault_dir)
    await maintenance.migrate_vault()
    assert embedder.embedded, "control: the first run must have re-embedded something"
    embedder.embedded.clear()

    report = await maintenance.migrate_vault()

    assert BACKFILL_PASSAGE_INPUT_BOUND not in report.backfills_applied
    assert embedder.embedded == []


async def test_only_the_oversize_document_is_re_embedded(
    graph_store, store, ingestion, embedder, minimal_config, tmp_vault_dir, legacy_vault
):
    fitting_before = [
        (c.heading_path, c.content, c.chunk_index, c.section_index)
        for c in await store.get_all_chunks(FITTING)
    ]

    await _maintenance(graph_store, store, ingestion, minimal_config, tmp_vault_dir).migrate_vault()

    assert embedder.embedded
    assert not any("fitting" in text for text in embedder.embedded)
    assert any("oversize" in text for text in embedder.embedded)
    fitting_after = [
        (c.heading_path, c.content, c.chunk_index, c.section_index)
        for c in await store.get_all_chunks(FITTING)
    ]
    assert fitting_after == fitting_before


async def test_rewritten_passages_keep_their_stored_metadata(
    graph_store, store, ingestion, minimal_config, tmp_vault_dir, legacy_vault
):
    original = {c.heading_path: c for c in legacy_vault}
    assert all(
        (c.doc_type, c.lifecycle_status, c.project) == ("ticket", "active", "CAS")
        for c in legacy_vault
    ), "control: the stored rows must carry metadata, or None == None passes vacuously"

    await _maintenance(graph_store, store, ingestion, minimal_config, tmp_vault_dir).migrate_vault()

    for chunk in await store.get_all_chunks(OVERSIZE):
        source = original[chunk.heading_path]
        assert (
            chunk.indexed_structure,
            chunk.doc_type,
            chunk.lifecycle_status,
            chunk.project,
        ) == (source.indexed_structure, source.doc_type, source.lifecycle_status, source.project)
    assert original["Doc > Long"].indexed_structure == "Long"


async def test_migrated_passages_match_what_ingest_now_writes(
    graph_store, store, ingestion, minimal_config, tmp_vault_dir, legacy_vault
):
    await _maintenance(graph_store, store, ingestion, minimal_config, tmp_vault_dir).migrate_vault()

    fresh = ingestion._chunk_projection(OVERSIZE, _projection(OVERSIZE, _oversize_body()))
    migrated = await store.get_all_chunks(OVERSIZE)

    def shape(chunks):
        return [(c.heading_path, c.section_index, c.content, c.chunk_index) for c in chunks]

    assert shape(migrated) == shape(fresh)


# ---------------------------------------------------------------------------
# The migration shares the server with ingest, and must not overwrite a
# document that is being re-indexed while it works.
# ---------------------------------------------------------------------------


def _shape(chunks):
    return [(c.heading_path, c.content, c.section_index) for c in chunks]


async def test_a_document_with_pipeline_work_in_flight_is_left_for_a_later_run(
    graph_store, store, ingestion, embedder, minimal_config, tmp_vault_dir, legacy_vault
):
    maintenance = _maintenance(graph_store, store, ingestion, minimal_config, tmp_vault_dir)
    assert ingestion._try_claim(OVERSIZE, "recompute") is None

    report = await maintenance.migrate_vault()

    assert BACKFILL_PASSAGE_INPUT_BOUND not in report.backfills_applied
    assert embedder.embedded == []
    assert _shape(await store.get_all_chunks(OVERSIZE)) == _shape(legacy_vault)

    ingestion._release_claim(OVERSIZE)
    report = await maintenance.migrate_vault()

    assert BACKFILL_PASSAGE_INPUT_BOUND in report.backfills_applied, (
        "control: the same document is divided once nothing holds it"
    )


@pytest.mark.parametrize(
    "status", [PipelineStatus.INDEXING_IN_PROGRESS, PipelineStatus.ABSTRACTION_IN_PROGRESS]
)
async def test_a_document_mid_pipeline_is_left_for_a_later_run(
    graph_store, store, ingestion, embedder, minimal_config, tmp_vault_dir, legacy_vault, status
):
    await graph_store.update_document(OVERSIZE, {"pipeline_status": status.value})

    report = await _maintenance(
        graph_store, store, ingestion, minimal_config, tmp_vault_dir
    ).migrate_vault()

    assert BACKFILL_PASSAGE_INPUT_BOUND not in report.backfills_applied
    assert embedder.embedded == []
    assert _shape(await store.get_all_chunks(OVERSIZE)) == _shape(legacy_vault)


async def test_passages_rewritten_while_the_division_embeds_are_not_overwritten(
    graph_store, store, ingestion, embedder, minimal_config, tmp_vault_dir, legacy_vault
):
    """The concurrent write keeps the stored shape -- same rows, same heading
    paths -- and changes only content, as re-ingesting an edited source does."""
    concurrent = [
        Chunk(
            document_id=OVERSIZE,
            heading_path=c.heading_path,
            content=f"re-indexed meanwhile {c.chunk_index}",
            embedding=[0.5] * EMBEDDING_DIM,
            chunk_index=c.chunk_index,
            section_index=c.chunk_index,
        )
        for c in legacy_vault
    ]

    async def reindex_meanwhile():
        embedder.during_embed = None
        await store.index_chunks(OVERSIZE, concurrent)

    embedder.during_embed = reindex_meanwhile

    report = await _maintenance(
        graph_store, store, ingestion, minimal_config, tmp_vault_dir
    ).migrate_vault()

    assert embedder.embedded, "control: the division reached its embed call"
    assert _shape(await store.get_all_chunks(OVERSIZE)) == _shape(concurrent)
    assert BACKFILL_PASSAGE_INPUT_BOUND not in report.backfills_applied


async def test_the_division_holds_the_document_while_it_embeds(
    graph_store, store, ingestion, embedder, minimal_config, tmp_vault_dir, legacy_vault
):
    observed = []

    async def try_to_claim():
        observed.append(ingestion._try_claim(OVERSIZE, "recompute"))

    embedder.during_embed = try_to_claim

    await _maintenance(graph_store, store, ingestion, minimal_config, tmp_vault_dir).migrate_vault()

    assert observed and observed[0] is not None, "a recompute could start mid-division"
    assert OVERSIZE not in ingestion._inflight, "the division's claim outlived it"


@pytest.mark.parametrize(
    "status",
    [
        PipelineStatus.ABSTRACTION_SKIPPED,
        PipelineStatus.ABSTRACTION_INTERRUPTED,
        PipelineStatus.FAILED,
    ],
)
async def test_every_terminal_status_is_divided(
    graph_store, store, ingestion, minimal_config, tmp_vault_dir, legacy_vault, status
):
    await graph_store.update_document(OVERSIZE, {"pipeline_status": status.value})

    report = await _maintenance(
        graph_store, store, ingestion, minimal_config, tmp_vault_dir
    ).migrate_vault()

    assert BACKFILL_PASSAGE_INPUT_BOUND in report.backfills_applied
    assert len(await store.get_all_chunks(OVERSIZE)) > len(legacy_vault)


async def test_a_document_left_for_a_later_run_is_logged_with_its_reason(
    graph_store, store, ingestion, minimal_config, tmp_vault_dir, legacy_vault, caplog
):
    await graph_store.update_document(
        OVERSIZE, {"pipeline_status": PipelineStatus.INDEXING_IN_PROGRESS.value}
    )
    assert ingestion._try_claim(FITTING, "recompute") is None

    with caplog.at_level("INFO", logger="sage.services.ingestion"):
        await _maintenance(
            graph_store, store, ingestion, minimal_config, tmp_vault_dir
        ).migrate_vault()

    left = [r.getMessage() for r in caplog.records if "left for a later" in r.getMessage()]
    assert any(OVERSIZE in m and "indexing_in_progress" in m for m in left), left
    assert not any(FITTING in m for m in left), (
        "a document the division would not have rewritten is not reported as left"
    )


async def test_a_lifecycle_stamp_during_the_division_is_not_reverted(
    graph_store, store, ingestion, embedder, minimal_config, tmp_vault_dir, legacy_vault
):
    """Archiving writes the new status onto the passage rows, which the division
    carries forward from the rows it read before embedding."""

    async def archive_meanwhile():
        embedder.during_embed = None
        await store.update_chunk_metadata(OVERSIZE, {"lifecycle_status": "archived"})

    embedder.during_embed = archive_meanwhile

    await _maintenance(graph_store, store, ingestion, minimal_config, tmp_vault_dir).migrate_vault()

    assert embedder.embedded, "control: the division reached its embed call"
    assert {c.lifecycle_status for c in await store.get_all_chunks(OVERSIZE)} == {"archived"}
