"""Marking documents whose empty projection settled before ingestion recorded it.

A document whose projection holds no readable text settles at
``abstraction_skipped``, and ingestion now records that in its abstraction
evidence as ``empty_readable_projection``, which the deferred-abstract sweep
reads to report it as ``no_readable_text`` instead of dispatching it. A document
that settled there before the mark was recorded carries none, so every sweep
dispatches it and counts the refusal as a failure.

The migration re-projects each such candidate's retained source and, where the
projection really is empty, records the evidence ingestion records today. A
candidate whose source now projects text, or cannot be brought to the same
bytes, is left as it is and not read again.

Each document here is ingested through an adapter that projects every source to
empty text, then has its mark removed, which is the state a document ingested
before the mark carries. The migration runs with the markdown adapter as
shipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.stubs import StubAbstractionProvider, StubEmbeddingProvider
from sage.models.coverage import EMPTY_READABLE_PROJECTION, has_empty_readable_projection
from sage.models.enums import PipelineStatus, ReabstractOutcome, SourceType
from sage.models.schemas import IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.maintenance import BACKFILL_EMPTY_READABLE_PROJECTION, MaintenanceService
from sage.source_adapters.base import ProjectionResult
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from tests.helpers.pipeline_wait import await_pipeline_idle
from tests.sage.test_ingestion import _EmptyTextStubAdapter

pytestmark = pytest.mark.asyncio


def blank(width: int) -> bytes:
    """Whitespace only, distinct per width so each source is its own content."""
    return b" " * width + b"\n\t\n"


TEXT = b"# Scan\n\nThe scanner later learned to read this page.\n"


class _ObservedMarkdown(MarkdownAdapter):
    """The markdown adapter as shipped, recording each source it projects."""

    def __init__(self) -> None:
        super().__init__()
        self.projected: list[str] = []

    async def project(self, source_path: Path, config: dict | None = None) -> ProjectionResult:
        self.projected.append(source_path.name)
        return await super().project(source_path, config)


class Vault:
    def __init__(self, graph_store, store, ingestion, config, root, pg_pool):
        self.graph_store = graph_store
        self.store = store
        self.ingestion = ingestion
        self.config = config
        self.root = Path(root)
        self.pg_pool = pg_pool

    async def ingest(self, name: str, content: bytes) -> str:
        (self.root / "sources" / name).write_bytes(content)
        result = await self.ingestion.ingest(
            IngestRequest(source=name, source_type=SourceType.MARKDOWN)
        )
        await await_pipeline_idle(self.graph_store, result.document.id, service=self.ingestion)
        return result.document.id

    async def unmarked(self, name: str, content: bytes) -> str:
        """Ingest to an empty projection, then remove the mark ingestion recorded."""
        document_id = await self.ingest(name, content)
        doc = await self.graph_store.get_document(document_id)
        assert doc.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED.value
        assert has_empty_readable_projection(doc.processing_coverage), "control: marked on ingest"
        async with self.pg_pool.connection() as conn:
            await conn.execute(
                "UPDATE documents SET processing_coverage = "
                "processing_coverage - 'abstraction_evidence' WHERE id = %s",
                (document_id,),
            )
        doc = await self.graph_store.get_document(document_id)
        assert not has_empty_readable_projection(doc.processing_coverage)
        assert not await self.store.has_chunks(document_id)
        return document_id

    def ship_adapters(self) -> _ObservedMarkdown:
        adapter = _ObservedMarkdown()
        self.ingestion._adapters = {SourceType.MARKDOWN: adapter}
        return adapter

    def maintenance(self) -> MaintenanceService:
        return MaintenanceService(
            vault_id=self.config.vault.id,
            graph_store=self.graph_store,
            config=self.config,
            registry_service=None,
            content_store=self.store,
            ingestion_service=self.ingestion,
            vault_dir=self.root,
        )

    async def migrate(self):
        return await self.maintenance().migrate_vault()

    async def row(self, document_id: str) -> tuple:
        """The stored state the migration must leave alone on a document it does not mark."""
        async with self.pg_pool.connection() as conn:
            cur = await conn.execute(
                "SELECT pipeline_status, processing_coverage, adapter_version, version_token "
                "FROM documents WHERE id = %s",
                (document_id,),
            )
            return await cur.fetchone()


def _service(graph_store, lock_manager, store, config, lifecycle_service) -> IngestionService:
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=store,
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
        config=config,
        source_adapters={SourceType.MARKDOWN: _EmptyTextStubAdapter()},
        lifecycle_service=lifecycle_service,
    )


@pytest.fixture
async def vault(
    graph_store, lock_manager, pg_pool, minimal_config, lifecycle_service, tmp_vault_dir
):
    store = PostgresContentStore(pg_pool)
    ingestion = _service(graph_store, lock_manager, store, minimal_config, lifecycle_service)
    return Vault(graph_store, store, ingestion, minimal_config, tmp_vault_dir, pg_pool)


async def _skipped_with_passages(vault: Vault) -> str:
    """A document holding passages that sits at abstraction_skipped for another reason."""
    vault.ingestion._adapters = {SourceType.MARKDOWN: MarkdownAdapter()}
    document_id = await vault.ingest("held.md", b"# Held\n\nHeld body text.\n")
    vault.ingestion._adapters = {SourceType.MARKDOWN: _EmptyTextStubAdapter()}
    await vault.graph_store.update_document(
        document_id,
        {"pipeline_status": PipelineStatus.ABSTRACTION_SKIPPED.value},
        advance_version=False,
    )
    assert await vault.store.has_chunks(document_id)
    return document_id


async def test_one_run_marks_only_the_unmarked_empty_document(vault):
    unmarked = await vault.unmarked("blank.md", blank(1))
    marked = await vault.ingest("scan.md", blank(2))
    held = await _skipped_with_passages(vault)
    marked_before = await vault.row(marked)
    held_before = await vault.row(held)
    adapter = vault.ship_adapters()

    report = await vault.migrate()

    assert BACKFILL_EMPTY_READABLE_PROJECTION in report.backfills_applied
    assert report.documents_not_repaired == []
    doc = await vault.graph_store.get_document(unmarked)
    assert has_empty_readable_projection(doc.processing_coverage)
    assert doc.processing_coverage.abstraction_evidence.limitations == [EMPTY_READABLE_PROJECTION]
    assert doc.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED.value
    assert await vault.row(marked) == marked_before
    assert await vault.row(held) == held_before
    assert adapter.projected == ["blank.md"]


async def test_the_mark_matches_what_ingestion_records(vault):
    unmarked = await vault.unmarked("blank.md", blank(3))
    reference = await vault.ingest("reference.md", blank(4))
    vault.ship_adapters()

    await vault.migrate()

    repaired = (await vault.graph_store.get_document(unmarked)).processing_coverage
    recorded = (await vault.graph_store.get_document(reference)).processing_coverage
    # The store numbers each publication's generation itself; everything the
    # writer supplies must match.
    assert repaired.abstraction_evidence.model_dump(exclude={"generation"}) == (
        recorded.abstraction_evidence.model_dump(exclude={"generation"})
    )


async def test_a_second_run_reads_no_source_and_changes_nothing(vault):
    unmarked = await vault.unmarked("blank.md", blank(5))
    readable = await vault.unmarked("readable.md", TEXT)
    first = vault.ship_adapters()
    await vault.migrate()
    assert sorted(first.projected) == ["blank.md", "readable.md"], "control: both were read"
    settled = {d: await vault.row(d) for d in (unmarked, readable)}
    second = vault.ship_adapters()

    report = await vault.migrate()

    assert BACKFILL_EMPTY_READABLE_PROJECTION not in report.backfills_applied
    assert report.documents_not_repaired == []
    assert second.projected == []
    assert {d: await vault.row(d) for d in settled} == settled


async def test_a_source_that_now_projects_text_is_left_unchanged(vault):
    readable = await vault.unmarked("readable.md", TEXT)
    before = await vault.row(readable)
    vault.ship_adapters()

    report = await vault.migrate()

    assert BACKFILL_EMPTY_READABLE_PROJECTION not in report.backfills_applied
    assert [entry.document_id for entry in report.documents_not_repaired] == [readable]
    assert await vault.row(readable) == before
    assert not await vault.store.has_chunks(readable)


async def test_a_changed_source_is_not_marked_and_not_read_again(vault):
    changed = await vault.unmarked("changed.md", blank(6))
    (vault.root / "sources" / "changed.md").write_bytes(blank(99))
    before = await vault.row(changed)
    first = vault.ship_adapters()

    report = await vault.migrate()

    assert first.projected == ["changed.md"], "control: the candidate was examined"
    assert [entry.document_id for entry in report.documents_not_repaired] == [changed]
    assert BACKFILL_EMPTY_READABLE_PROJECTION not in report.backfills_applied
    assert await vault.row(changed) == before
    second = vault.ship_adapters()
    again = await vault.migrate()
    assert second.projected == []
    assert again.documents_not_repaired == []


async def test_a_skip_another_adapter_recorded_is_examined_and_cleared(vault):
    unmarked = await vault.unmarked("blank.md", blank(13))
    await vault.graph_store.record_reprojection_skip(unmarked, "0.0.0-older", "unreadable then")
    adapter = vault.ship_adapters()

    report = await vault.migrate()

    assert adapter.projected == ["blank.md"]
    assert BACKFILL_EMPTY_READABLE_PROJECTION in report.backfills_applied
    doc = await vault.graph_store.get_document(unmarked)
    assert has_empty_readable_projection(doc.processing_coverage)
    assert unmarked not in await vault.graph_store.reprojection_skips()


async def test_a_superseded_mark_is_reported_and_tried_again(vault, monkeypatch):
    unmarked = await vault.unmarked("blank.md", blank(12))
    real_publish = vault.store.publish_processing

    async def superseded(document_id, attempt, dimensions, **kwargs):
        if set(dimensions) == {"abstraction_evidence"}:
            return False
        return await real_publish(document_id, attempt, dimensions, **kwargs)

    monkeypatch.setattr(vault.store, "publish_processing", superseded)
    vault.ship_adapters()

    report = await vault.migrate()

    assert BACKFILL_EMPTY_READABLE_PROJECTION not in report.backfills_applied
    assert [entry.document_id for entry in report.documents_not_repaired] == [unmarked]
    assert unmarked not in await vault.graph_store.reprojection_skips()
    monkeypatch.setattr(vault.store, "publish_processing", real_publish)
    retry = vault.ship_adapters()
    again = await vault.migrate()
    assert retry.projected == ["blank.md"]
    assert BACKFILL_EMPTY_READABLE_PROJECTION in again.backfills_applied
    doc = await vault.graph_store.get_document(unmarked)
    assert has_empty_readable_projection(doc.processing_coverage)


async def test_disabled_abstraction_records_no_mark(vault):
    unmarked = await vault.unmarked("blank.md", blank(7))
    before = await vault.row(unmarked)
    vault.config.abstraction.enabled = False
    adapter = vault.ship_adapters()

    report = await vault.migrate()

    assert BACKFILL_EMPTY_READABLE_PROJECTION not in report.backfills_applied
    assert adapter.projected == []
    assert await vault.row(unmarked) == before


async def test_documents_in_other_pipeline_states_are_not_candidates(vault):
    failed = await vault.unmarked("failed.md", blank(8))
    await vault.graph_store.update_document(
        failed, {"pipeline_status": PipelineStatus.FAILED.value}, advance_version=False
    )
    before = await vault.row(failed)
    adapter = vault.ship_adapters()

    report = await vault.migrate()

    assert BACKFILL_EMPTY_READABLE_PROJECTION not in report.backfills_applied
    assert adapter.projected == []
    assert await vault.row(failed) == before


async def test_the_mark_is_the_shared_limitation(vault, monkeypatch):
    unmarked = await vault.unmarked("blank.md", blank(9))
    vault.ship_adapters()
    monkeypatch.setattr("sage.services.ingestion.EMPTY_READABLE_PROJECTION", "relabelled")

    await vault.migrate()

    doc = await vault.graph_store.get_document(unmarked)
    assert doc.processing_coverage.abstraction_evidence.limitations == ["relabelled"]


async def test_a_following_sweep_reports_the_repaired_document_as_unreadable(vault):
    unmarked = await vault.unmarked("blank.md", blank(10))
    marked = await vault.ingest("scan.md", blank(11))
    held = await _skipped_with_passages(vault)
    vault.ship_adapters()
    await vault.migrate()

    report = await vault.maintenance().reabstract_deferred()

    outcomes = {entry.document_id: entry.outcome for entry in report.entries}
    assert outcomes[unmarked] == ReabstractOutcome.NO_READABLE_TEXT
    assert outcomes[marked] == ReabstractOutcome.NO_READABLE_TEXT
    assert outcomes[held] == ReabstractOutcome.SUCCESS
    assert report.no_readable_text_count == 2
    assert report.failed_count == 0
