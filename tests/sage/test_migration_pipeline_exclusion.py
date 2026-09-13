"""migrate_vault and pipeline work exclude each other on a vault.

The migration's backfills rewrite stored passages without per-document
concurrency control, so they are safe only while no pipeline work runs on the
vault. ``migrate_vault`` refuses while any is queued, claimed or running --
including an inline ingest, which holds no claim -- and while it runs, ingest,
reabstract and recompute_pipeline are refused. Each test below names the rival
predicate it is there to reject.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from sage.adapters.stubs import StubAbstractionProvider, StubContentStore, StubEmbeddingProvider
from sage.api.errors import PipelineWorkInFlightError, VaultMigrationInFlightError
from sage.config import VaultConfig
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import IngestPreview, IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.maintenance import MaintenanceService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.storage.locks import DocumentLockManager
from tests.sage.test_abstraction_queue import (
    _await_terminal,
    _GatedAbstractionProvider,
    _seed_indexed_doc,
)
from tests.sage.test_ingestion import _create_test_file

pytestmark = pytest.mark.asyncio

STALE = "a failure this document has since recovered from"


def _within(awaitable):
    """Bound a call a broken exclusion would leave waiting on a held gate."""
    return asyncio.wait_for(awaitable, timeout=5.0)


class _HeldBackfill:
    """Stands in for the passage-division backfill, optionally held open.

    Replacing the backfill, rather than anything in the exclusion, is what lets
    a test observe the vault while a migration is between its start and its end.
    """

    def __init__(self, *, hold: bool = False, raises: Exception | None = None) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        if not hold:
            self.gate.set()
        self.raises = raises

    async def __call__(self) -> int:
        self.entered.set()
        await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        return 0


class _GatedEmbedder(StubEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.entered.set()
        await self.gate.wait()
        return await super().embed(texts)


class _GatedAdapter(MarkdownAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def project(self, path, config):
        self.entered.set()
        await self.gate.wait()
        return await super().project(path, config)


def _maintenance(graph_store, config, ingestion, backfill: _HeldBackfill) -> MaintenanceService:
    maintenance = MaintenanceService(
        vault_id=config.vault.id,
        graph_store=graph_store,
        config=config,
        registry_service=None,
        content_store=StubContentStore(),
        ingestion_service=ingestion,
    )
    maintenance._divide_passages_over_input_bound = backfill
    return maintenance


@pytest.fixture
def backfill():
    return _HeldBackfill()


@pytest.fixture
def maintenance(graph_store, minimal_config, ingestion_service, backfill):
    return _maintenance(graph_store, minimal_config, ingestion_service, backfill)


@pytest.fixture(autouse=True)
async def _stop_fixture_worker(ingestion_service):
    yield
    await ingestion_service.stop_worker()


async def _seed_stale_error(graph_store, service, tmp_vault_dir) -> str:
    """A settled document still carrying an old pipeline_error.

    The migration's first backfill clears it, so it still being there shows
    that the refused migration ran no backfill at all.
    """
    doc_id = await _seed_indexed_doc(service, tmp_vault_dir, "samples/stale.md")
    await graph_store.update_document(doc_id, {"pipeline_error": STALE})
    return doc_id


def _assert_pipeline_work_refusal(excinfo, config) -> None:
    err = excinfo.value
    assert err.code == "pipeline_work_in_flight"
    assert err.status_code == 409
    assert err.detail == {"vault_id": config.vault.id}


async def _assert_no_backfill_ran(graph_store, stale_id: str) -> None:
    assert (await graph_store.get_document(stale_id)).pipeline_error == STALE


# ---------------------------------------------------------------------------
# migrate_vault refuses while pipeline work is in flight
# ---------------------------------------------------------------------------


async def test_refused_while_a_job_is_queued(
    graph_store, ingestion_service, maintenance, minimal_config, tmp_vault_dir
):
    """Rival: refusing only while the worker is mid-job. Here the job is queued
    and the worker has not yet been scheduled, so nothing is running."""
    stale = await _seed_stale_error(graph_store, ingestion_service, tmp_vault_dir)
    queued = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/queued.md")

    await ingestion_service.reabstract(queued)
    assert ingestion_service._abstraction_queue.qsize() == 1, "control: the job is still queued"
    with pytest.raises(PipelineWorkInFlightError) as excinfo:
        await maintenance.migrate_vault()

    _assert_pipeline_work_refusal(excinfo, minimal_config)
    await _assert_no_backfill_ran(graph_store, stale)
    await _await_terminal(graph_store, ingestion_service, queued)


async def test_refused_while_a_claimed_job_runs(
    graph_store, ingestion_service, maintenance, minimal_config, tmp_vault_dir
):
    """Rival: refusing while the queue holds jobs. A running job has already
    been taken off the queue."""
    stale = await _seed_stale_error(graph_store, ingestion_service, tmp_vault_dir)
    running = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/running.md")
    gated = _GatedAbstractionProvider()
    ingestion_service._abstraction = gated

    await ingestion_service.reabstract(running)
    await asyncio.wait_for(gated.entered.wait(), timeout=2.0)
    assert ingestion_service._abstraction_queue.qsize() == 0, "control: the job was dequeued"

    with pytest.raises(PipelineWorkInFlightError) as excinfo:
        await maintenance.migrate_vault()

    _assert_pipeline_work_refusal(excinfo, minimal_config)
    await _assert_no_backfill_ran(graph_store, stale)
    gated.gate.set()
    await _await_terminal(graph_store, ingestion_service, running)


async def test_refused_while_an_inline_ingest_indexes(
    graph_store, ingestion_service, maintenance, minimal_config, tmp_vault_dir
):
    """Rival: the claim registry alone. An inline ingest holds no claim."""
    stale = await _seed_stale_error(graph_store, ingestion_service, tmp_vault_dir)
    embedder = _GatedEmbedder()
    ingestion_service._embedding = embedder
    _create_test_file(tmp_vault_dir, "samples/inline.md", "# Inline\n\nIndexed inline.")
    request = IngestRequest(source="samples/inline.md", source_type=SourceType.MARKDOWN)

    task = asyncio.create_task(ingestion_service.ingest(request, wait_for_pipeline=True))
    await asyncio.wait_for(embedder.entered.wait(), timeout=2.0)
    assert not ingestion_service._inflight, "control: the inline path holds no claim"

    with pytest.raises(PipelineWorkInFlightError) as excinfo:
        await maintenance.migrate_vault()

    _assert_pipeline_work_refusal(excinfo, minimal_config)
    await _assert_no_backfill_ran(graph_store, stale)
    embedder.gate.set()
    await task


async def test_refused_while_an_ingest_projects_before_any_claim(
    graph_store, ingestion_service, maintenance, minimal_config, tmp_vault_dir
):
    """Rival: counting only the inline Stages 2-3. A queued ingest writes its
    document before it claims, while it is still projecting."""
    stale = await _seed_stale_error(graph_store, ingestion_service, tmp_vault_dir)
    adapter = _GatedAdapter()
    ingestion_service._adapters[SourceType.MARKDOWN] = adapter
    _create_test_file(tmp_vault_dir, "samples/projecting.md", "# Projecting\n\nStill reading.")
    request = IngestRequest(source="samples/projecting.md", source_type=SourceType.MARKDOWN)

    task = asyncio.create_task(ingestion_service.ingest(request, wait_for_pipeline=False))
    await asyncio.wait_for(adapter.entered.wait(), timeout=2.0)

    with pytest.raises(PipelineWorkInFlightError) as excinfo:
        await maintenance.migrate_vault()

    _assert_pipeline_work_refusal(excinfo, minimal_config)
    await _assert_no_backfill_ran(graph_store, stale)
    adapter.gate.set()
    result = await task
    await _await_terminal(graph_store, ingestion_service, result.document.id)


async def test_refused_while_a_deferred_reabstract_pass_runs(
    graph_store, ingestion_service, maintenance, minimal_config, tmp_vault_dir
):
    """Rival: consulting the ingestion service alone. A deferred pass between
    two documents holds no claim."""
    stale = await _seed_stale_error(graph_store, ingestion_service, tmp_vault_dir)
    await maintenance._reabstract_lock.acquire()
    try:
        assert not ingestion_service._inflight
        with pytest.raises(PipelineWorkInFlightError) as excinfo:
            await maintenance.migrate_vault()
    finally:
        maintenance._reabstract_lock.release()

    _assert_pipeline_work_refusal(excinfo, minimal_config)
    await _assert_no_backfill_ran(graph_store, stale)


# ---------------------------------------------------------------------------
# While migrate_vault runs, pipeline entry points are refused
# ---------------------------------------------------------------------------


async def _start_held_migration(maintenance, backfill) -> tuple[asyncio.Task, datetime]:
    backfill.gate.clear()
    started = datetime.now(timezone.utc)
    task = asyncio.create_task(maintenance.migrate_vault())
    await asyncio.wait_for(backfill.entered.wait(), timeout=2.0)
    return task, started


def _assert_migration_refusal(excinfo, config, started: datetime) -> None:
    err = excinfo.value
    assert err.code == "vault_migration_in_flight"
    assert err.status_code == 409
    assert err.detail["vault_id"] == config.vault.id
    start_time = datetime.fromisoformat(err.detail["start_time"])
    assert started <= start_time <= datetime.now(timezone.utc)


ENTRY_POINTS = ["ingest_inline", "ingest_queued", "ingest_dry_run", "reabstract", "recompute"]


async def _call_entry_point(name, service, maintenance, tmp_vault_dir, doc_id):
    if name.startswith("ingest"):
        _create_test_file(tmp_vault_dir, "samples/refused.md", "# Refused\n\nNot admitted.")
        request = IngestRequest(
            source="samples/refused.md",
            source_type=SourceType.MARKDOWN,
            dry_run=name == "ingest_dry_run",
        )
        return await service.ingest(request, wait_for_pipeline=name == "ingest_inline")
    if name == "reabstract":
        return await service.reabstract(doc_id)
    if name == "recompute":
        return await service.recompute_pipeline(doc_id)
    raise AssertionError(name)


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
async def test_each_entry_point_is_refused_mid_migration(
    graph_store,
    ingestion_service,
    maintenance,
    backfill,
    minimal_config,
    tmp_vault_dir,
    entry_point,
):
    """Rival: a refusal that follows the entry point's first write. Nothing the
    call would have done -- a claim, a document, a status stamp -- is left."""
    doc_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/target.md")
    documents_before = len(await graph_store.list_all_documents())
    migration, started = await _start_held_migration(maintenance, backfill)

    with pytest.raises(VaultMigrationInFlightError) as excinfo:
        await _within(
            _call_entry_point(entry_point, ingestion_service, maintenance, tmp_vault_dir, doc_id)
        )

    _assert_migration_refusal(excinfo, minimal_config, started)
    assert not ingestion_service._inflight
    assert len(await graph_store.list_all_documents()) == documents_before
    doc = await graph_store.get_document(doc_id)
    assert doc.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE
    backfill.gate.set()
    await migration


async def test_a_deferred_reabstract_pass_is_refused_mid_migration(
    ingestion_service, maintenance, backfill, minimal_config
):
    migration, started = await _start_held_migration(maintenance, backfill)

    with pytest.raises(VaultMigrationInFlightError) as excinfo:
        await _within(maintenance.reabstract_deferred())

    _assert_migration_refusal(excinfo, minimal_config, started)
    assert not maintenance._reabstract_lock.locked()
    backfill.gate.set()
    await migration


async def test_a_second_migration_is_refused_while_one_runs(
    ingestion_service, maintenance, backfill, minimal_config
):
    """Rival: a flag each migration sets and clears. The first to finish would
    lift the exclusion while the second still ran."""
    migration, started = await _start_held_migration(maintenance, backfill)

    with pytest.raises(VaultMigrationInFlightError) as excinfo:
        await _within(maintenance.migrate_vault())

    _assert_migration_refusal(excinfo, minimal_config, started)
    backfill.gate.set()
    await migration
    await maintenance.migrate_vault()


@pytest.mark.parametrize("entry_point", ["reabstract", "recompute"])
async def test_a_call_awaiting_before_its_claim_is_refused_at_the_claim(
    graph_store,
    ingestion_service,
    maintenance,
    backfill,
    minimal_config,
    tmp_vault_dir,
    monkeypatch,
    entry_point,
):
    """Rival: checking at the top of the method. A migration can start while
    the call awaits its document read, and the call would then claim anyway."""
    doc_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/racing.md")
    reading = asyncio.Event()
    release = asyncio.Event()
    get_document = graph_store.get_document

    async def held_get_document(document_id):
        if document_id == doc_id and not reading.is_set():
            reading.set()
            await release.wait()
        return await get_document(document_id)

    monkeypatch.setattr(graph_store, "get_document", held_get_document)
    call = asyncio.create_task(
        _call_entry_point(entry_point, ingestion_service, maintenance, tmp_vault_dir, doc_id)
    )
    await asyncio.wait_for(reading.wait(), timeout=2.0)
    migration, started = await _start_held_migration(maintenance, backfill)

    release.set()
    with pytest.raises(VaultMigrationInFlightError) as excinfo:
        await _within(call)

    _assert_migration_refusal(excinfo, minimal_config, started)
    assert not ingestion_service._inflight
    backfill.gate.set()
    await migration


# ---------------------------------------------------------------------------
# The exclusion is released however the migration ends
# ---------------------------------------------------------------------------


async def test_pipeline_work_is_admitted_after_a_migration_returns(
    graph_store, ingestion_service, maintenance, tmp_vault_dir
):
    await maintenance.migrate_vault()

    doc_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/after.md")

    assert (await graph_store.get_document(doc_id)).pipeline_status == (
        PipelineStatus.ABSTRACTION_COMPLETE
    )


async def test_the_exclusion_is_released_when_a_backfill_raises(
    graph_store, ingestion_service, minimal_config, tmp_vault_dir
):
    """Rival: releasing only on the normal path out of the migration."""
    failure = RuntimeError("backfill failed")
    maintenance = _maintenance(
        graph_store, minimal_config, ingestion_service, _HeldBackfill(raises=failure)
    )

    with pytest.raises(RuntimeError) as excinfo:
        await maintenance.migrate_vault()
    assert excinfo.value is failure

    doc_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/released.md")
    await ingestion_service.reabstract(doc_id)
    await _await_terminal(graph_store, ingestion_service, doc_id)
    maintenance._divide_passages_over_input_bound = _HeldBackfill()
    await maintenance.migrate_vault()


# ---------------------------------------------------------------------------
# The exclusion is per vault
# ---------------------------------------------------------------------------


async def test_a_migration_on_one_vault_does_not_refuse_another(
    graph_store, maintenance, backfill, minimal_vault_config_dict, tmp_vault_dir
):
    """Rival: exclusion state shared across every vault in the process."""
    other_config = VaultConfig.model_validate(
        {
            **minimal_vault_config_dict,
            "vault": {**minimal_vault_config_dict["vault"], "id": "other"},
        }
    )
    other = IngestionService(
        graph_store=graph_store,
        lock_manager=DocumentLockManager(),
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
        config=other_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )
    migration, _ = await _start_held_migration(maintenance, backfill)
    _create_test_file(tmp_vault_dir, "samples/other.md", "# Other\n\nA different vault.")

    result = await other.ingest(
        IngestRequest(source="samples/other.md", source_type=SourceType.MARKDOWN, dry_run=True)
    )

    assert isinstance(result, IngestPreview)
    backfill.gate.set()
    await migration
