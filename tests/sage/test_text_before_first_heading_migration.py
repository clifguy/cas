"""Recovering the text documents carry before their first heading.

A document indexed before that text had a passage holds none of it, and its
stored passages cannot supply it. The migration re-projects the source, and when
the projection carries such text it adds that text as the document's first
passage. The passages already stored are kept exactly as they read, so every
heading path, section read and cached address resolves as before, and nothing is
re-abstracted.

Each document here is ingested through an adapter that withholds the preamble --
which is how the passage builder stored every document before -- and the
migration then runs with the adapter as shipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import Chunk
from sage.adapters.stubs import StubAbstractionProvider, StubEmbeddingProvider
from sage.models.enums import PipelineStatus, RetrievalMode, SourceType
from sage.models.schemas import DiscoverRequest, IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.maintenance import BACKFILL_TEXT_BEFORE_FIRST_HEADING, MaintenanceService
from sage.services.retrieval import RetrievalService
from sage.services.utilities import UtilitiesService
from sage.source_adapters.base import ProjectionResult
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.source_adapters.pdf_adapter import PdfAdapter
from sage.storage.postgres.schema import EMBEDDING_DIM
from sage.vault_source_binding import FilesystemVaultSourceStore
from tests.helpers.pipeline_wait import await_pipeline_idle
from tests.sage.test_adapters import _make_pdf_with_outline, _make_pdf_with_pages, requires_pdf

pytestmark = pytest.mark.asyncio

LEAD = "Larkspur branch holds the quarterly archive."
BODY = "# Guide\n\nGuide body.\n\n## Part\n\nPart body.\n"


class _RecordingEmbedder(StubEmbeddingProvider):
    """Records what it embeds, and can act between a read and its rewrite."""

    def __init__(self) -> None:
        super().__init__()
        self.embedded: list[str] = []
        self.during_embed = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        if self.during_embed is not None:
            await self.during_embed()
        return await super().embed(texts)


class _CountingStore(PostgresContentStore):
    def __init__(self, pool) -> None:
        super().__init__(pool)
        self.replacements = 0

    async def replace_chunks_if_unchanged(self, document_id, expected, chunks) -> bool:
        self.replacements += 1
        return await super().replace_chunks_if_unchanged(document_id, expected, chunks)


class _CountingAbstraction(StubAbstractionProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.calls += 1
        return await super().generate_abstract(text, max_tokens, doc_type)


def _observed(base: type, *, withhold: bool):
    """An adapter of ``base`` that records each source it projects."""

    class Observed(base):
        def __init__(self) -> None:
            super().__init__()
            self.projected: list[str] = []

        async def project(self, source_path: Path, config: dict | None = None) -> ProjectionResult:
            projection = await super().project(source_path, config)
            self.projected.append(source_path.name)
            if withhold:
                projection.preamble = ""
            return projection

    return Observed()


class Vault:
    def __init__(self, graph_store, store, embedder, abstraction, ingestion, config, root):
        self.graph_store = graph_store
        self.store = store
        self.embedder = embedder
        self.abstraction = abstraction
        self.ingestion = ingestion
        self.config = config
        self.root = Path(root)

    async def ingest(self, name: str, content: bytes, source_type: SourceType) -> str:
        (self.root / "sources" / name).write_bytes(content)
        result = await self.ingestion.ingest(IngestRequest(source=name, source_type=source_type))
        await await_pipeline_idle(self.graph_store, result.document.id, service=self.ingestion)
        return result.document.id

    def ship_adapters(self) -> dict[SourceType, object]:
        """Swap in the adapters as shipped, each recording what it projects."""
        adapters = {
            SourceType.MARKDOWN: _observed(MarkdownAdapter, withhold=False),
            SourceType.PDF: _observed(PdfAdapter, withhold=False),
        }
        self.ingestion._adapters = adapters
        self.embedder.embedded.clear()
        self.store.replacements = 0
        self.abstraction.calls = 0
        return adapters

    async def migrate(self):
        return await MaintenanceService(
            vault_id=self.config.vault.id,
            graph_store=self.graph_store,
            config=self.config,
            registry_service=None,
            content_store=self.store,
            ingestion_service=self.ingestion,
            vault_dir=self.root,
        ).migrate_vault()

    def utilities(self) -> UtilitiesService:
        return UtilitiesService(
            graph_store=self.graph_store,
            content_store=self.store,
            embedding_provider=StubEmbeddingProvider(),
            config=self.config,
        )


@pytest.fixture
async def vault(
    graph_store, lock_manager, pg_pool, minimal_config, lifecycle_service, tmp_vault_dir
):
    store = _CountingStore(pg_pool)
    embedder = _RecordingEmbedder()
    abstraction = _CountingAbstraction()
    ingestion = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=store,
        embedding_provider=embedder,
        abstraction_provider=abstraction,
        config=minimal_config,
        source_adapters={
            SourceType.MARKDOWN: _observed(MarkdownAdapter, withhold=True),
            SourceType.PDF: _observed(PdfAdapter, withhold=True),
        },
        lifecycle_service=lifecycle_service,
    )
    return Vault(
        graph_store, store, embedder, abstraction, ingestion, minimal_config, tmp_vault_dir
    )


async def _led(vault: Vault) -> str:
    return await vault.ingest("led.md", f"{LEAD}\n\n{BODY}".encode(), SourceType.MARKDOWN)


def _shape(chunks: list[Chunk]) -> list[tuple[str, str, int | None]]:
    return [(c.heading_path, c.content, c.section_index) for c in chunks]


async def test_the_text_before_the_first_heading_becomes_the_first_passage(vault):
    led = await _led(vault)
    before = await vault.store.get_all_chunks(led)
    before_paths = await vault.store.get_heading_paths(led)
    utilities = vault.utilities()
    before_sections = {p: (await utilities.read_section(led, p)).section_text for p in before_paths}
    before_abstract = (await vault.graph_store.get_document(led)).semantic_abstract
    assert "" not in before_paths, "control: the seeded document must lack the passage"
    assert before_abstract, "control: the document must carry an abstract to preserve"
    vault.ship_adapters()

    report = await vault.migrate()

    assert BACKFILL_TEXT_BEFORE_FIRST_HEADING in report.backfills_applied
    after = await vault.store.get_all_chunks(led)
    assert (after[0].heading_path, after[0].content, after[0].section_index) == ("", LEAD, 0)
    assert [(c.heading_path, c.content) for c in after[1:]] == [
        (c.heading_path, c.content) for c in before
    ]
    assert await vault.store.get_heading_paths(led) == ["", *before_paths]
    for path, text in before_sections.items():
        assert (await utilities.read_section(led, path)).section_text == text
    assert (await utilities.read_section(led, "")).section_text == LEAD
    assert (await vault.graph_store.get_document(led)).semantic_abstract == before_abstract
    assert vault.abstraction.calls == 0


async def test_a_second_run_rewrites_nothing(vault):
    await _led(vault)
    vault.ship_adapters()
    await vault.migrate()
    assert vault.store.replacements == 1, "control: the first run must have rewritten"
    vault.embedder.embedded.clear()
    vault.store.replacements = 0

    report = await vault.migrate()

    assert BACKFILL_TEXT_BEFORE_FIRST_HEADING not in report.backfills_applied
    assert vault.embedder.embedded == []
    assert vault.store.replacements == 0


async def test_a_document_opening_at_a_heading_is_read_and_left_alone(vault):
    await _led(vault)
    plain = await vault.ingest(
        "plain.md", b"# Plain\n\nPlain sentinel body.\n", SourceType.MARKDOWN
    )
    before = _shape(await vault.store.get_all_chunks(plain))
    adapters = vault.ship_adapters()

    await vault.migrate()

    assert "plain.md" in adapters[SourceType.MARKDOWN].projected
    assert _shape(await vault.store.get_all_chunks(plain)) == before
    assert not any("Plain sentinel" in text for text in vault.embedder.embedded)
    assert any(LEAD in text for text in vault.embedder.embedded), (
        "control: the affected document in the same run was re-embedded"
    )


async def test_a_headingless_document_is_not_projected(vault):
    flat = await vault.ingest(
        "flat.md", b"Only a paragraph.\n\nAnd another.\n", SourceType.MARKDOWN
    )
    await _led(vault)
    before = _shape(await vault.store.get_all_chunks(flat))
    assert [path for path, _, _ in before] == [""], "control: the flat document's one passage"
    adapters = vault.ship_adapters()

    await vault.migrate()

    projected = adapters[SourceType.MARKDOWN].projected
    assert "led.md" in projected, "control: the migration projected its candidates"
    assert "flat.md" not in projected
    assert _shape(await vault.store.get_all_chunks(flat)) == before


@requires_pdf
async def test_a_pdf_without_an_outline_is_not_projected(vault, tmp_path):
    flat = await vault.ingest(
        "flat.pdf",
        _make_pdf_with_pages(
            tmp_path / "flat.pdf", [["PAGE_ONE"], ["PAGE_TWO"]], "Flat"
        ).read_bytes(),
        SourceType.PDF,
    )
    led = await vault.ingest(
        "led.pdf",
        _make_pdf_with_outline(
            tmp_path / "led.pdf",
            outline=[(1, "Intro", 1)],
            pages=[["COVER_PAGE_DELTA"], ["INTRO_BODY"]],
        ).read_bytes(),
        SourceType.PDF,
    )
    before = _shape(await vault.store.get_all_chunks(flat))
    adapters = vault.ship_adapters()

    report = await vault.migrate()

    assert BACKFILL_TEXT_BEFORE_FIRST_HEADING in report.backfills_applied
    first = (await vault.store.get_all_chunks(led))[0]
    assert (first.heading_path, first.content) == ("", "COVER_PAGE_DELTA")
    assert adapters[SourceType.PDF].projected == ["led.pdf"]
    assert _shape(await vault.store.get_all_chunks(flat)) == before


async def test_a_source_changed_since_it_was_indexed_is_left_for_a_later_run(vault, caplog):
    led = await _led(vault)
    doc = await vault.graph_store.get_document(led)
    (vault.root / "sources" / doc.source_path).write_text(f"Edited lead.\n\n{BODY}")
    before = _shape(await vault.store.get_all_chunks(led))
    vault.ship_adapters()

    with caplog.at_level("INFO", logger="sage.services.ingestion"):
        report = await vault.migrate()

    assert BACKFILL_TEXT_BEFORE_FIRST_HEADING not in report.backfills_applied
    assert _shape(await vault.store.get_all_chunks(led)) == before
    assert vault.embedder.embedded == []
    left = [r.getMessage() for r in caplog.records if "left for a later" in r.getMessage()]
    assert any(led in message for message in left), left


@pytest.mark.parametrize("hold", ["indexing_in_progress", "claimed"])
async def test_a_document_with_pipeline_work_is_left_for_a_later_run(vault, hold):
    led = await _led(vault)
    if hold == "claimed":
        assert vault.ingestion._try_claim(led, "recompute") is None
    else:
        await vault.graph_store.update_document(
            led, {"pipeline_status": PipelineStatus.INDEXING_IN_PROGRESS.value}
        )
    before = _shape(await vault.store.get_all_chunks(led))
    vault.ship_adapters()

    report = await vault.migrate()

    assert BACKFILL_TEXT_BEFORE_FIRST_HEADING not in report.backfills_applied
    assert _shape(await vault.store.get_all_chunks(led)) == before
    assert vault.embedder.embedded == []

    if hold == "claimed":
        vault.ingestion._release_claim(led)
    else:
        await vault.graph_store.update_document(
            led, {"pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE.value}
        )
    report = await vault.migrate()

    assert BACKFILL_TEXT_BEFORE_FIRST_HEADING in report.backfills_applied, (
        "control: the same document is recovered once nothing holds it"
    )


async def test_passages_rewritten_while_the_recovery_embeds_are_not_overwritten(vault):
    led = await _led(vault)
    stored = await vault.store.get_all_chunks(led)
    concurrent = [
        Chunk(
            document_id=led,
            heading_path=c.heading_path,
            content=f"re-indexed meanwhile {c.chunk_index}",
            embedding=[0.5] * EMBEDDING_DIM,
            chunk_index=c.chunk_index,
            section_index=c.chunk_index,
        )
        for c in stored
    ]

    async def reindex_meanwhile():
        vault.embedder.during_embed = None
        await vault.store.index_chunks(led, concurrent)

    vault.ship_adapters()
    vault.embedder.during_embed = reindex_meanwhile

    report = await vault.migrate()

    assert vault.embedder.embedded, "control: the recovery reached its embed call"
    assert _shape(await vault.store.get_all_chunks(led)) == _shape(concurrent)
    assert BACKFILL_TEXT_BEFORE_FIRST_HEADING not in report.backfills_applied


class _InMemorySourceStore(FilesystemVaultSourceStore):
    """Serves retained sources from memory, as a non-filesystem binding does."""

    def __init__(self, sources: dict[str, bytes]) -> None:
        super().__init__(Path("/unused/vault_root"))
        self.sources = sources
        self.reads: list[str] = []

    def source_exists(self, vault_id, storage_root, source_path):
        return source_path in self.sources

    def read_source(self, vault_id, storage_root, source_path):
        self.reads.append(source_path)
        return self.sources[source_path]


async def test_a_source_held_only_by_the_source_store_is_recovered(vault, monkeypatch):
    from sage.services.vault_source_errors import wrap_vault_source_store

    led = await _led(vault)
    doc = await vault.graph_store.get_document(led)
    local = vault.root / "sources" / doc.source_path
    remote = _InMemorySourceStore({doc.source_path: local.read_bytes()})
    local.unlink()
    assert not local.exists()
    monkeypatch.setattr(
        "sage.mcp_init.resolve_stack_vault_source_store",
        lambda *args, **kwargs: wrap_vault_source_store(remote),
    )
    vault.ship_adapters()

    report = await vault.migrate()

    assert remote.reads == [doc.source_path]
    assert BACKFILL_TEXT_BEFORE_FIRST_HEADING in report.backfills_applied
    assert (await vault.store.get_all_chunks(led))[0].content == LEAD


async def test_the_recovered_passage_carries_the_document_scalars(vault):
    led = await _led(vault)
    doc = await vault.graph_store.get_document(led)
    assert doc.doc_type and doc.lifecycle_status, "control: scalars to carry"
    vault.ship_adapters()

    await vault.migrate()

    first = (await vault.store.get_all_chunks(led))[0]
    assert first.heading_path == ""
    assert first.indexed_structure == ""
    assert (first.doc_type, first.lifecycle_status, first.project) == (
        doc.doc_type,
        str(doc.lifecycle_status),
        doc.project,
    )
    retrieval = RetrievalService(
        graph_store=vault.graph_store,
        content_store=vault.store,
        embedding_provider=StubEmbeddingProvider(),
        config=vault.config,
    )
    response = await retrieval.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="Larkspur",
            filters={"doc_type": doc.doc_type},
            limit=10,
        )
    )
    assert [h.document.id for h in response.results] == [led]
