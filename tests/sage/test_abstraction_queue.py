"""Persistent off-loop abstraction queue.

Locks in the scheduling layer that sits on top of the off-loop abstraction
executor: a single per-vault worker drains an in-memory queue, so concurrent
ingests enqueue-and-return, abstraction failures retry with bounded backoff, and
documents left non-terminal by a crash are recovered on startup.

Design invariants pinned here:

* Ingest ``wait_for_pipeline=False`` enqueues and returns a non-terminal
  status; a single worker drains one job at a time (AC1, AC2).
* Failed abstractions retry with bounded backoff; terminal failure after
  ``max_attempts`` stamps a structured ``pipeline_error`` (AC3).
* Ingest-abstract, ``reabstract``, and ``recompute_pipeline`` share one
  in-flight claim so the same document is never abstracted twice (AC4).
* Startup recovery re-derives pending work from ``pipeline_status`` — the
  graph store is the durable substrate; the queue itself is in-memory
  (AC5, AC6).

The ``wait_for_pipeline=True`` inline path is deliberately NOT routed through
the queue and keeps its fail-fast (no-retry) semantics; ``test_..._inline``
pins that boundary.
"""

import asyncio

import pytest

from sage.adapters.interfaces import AbstractionProvider
from sage.adapters.stubs import StubContentStore, StubEmbeddingProvider
from sage.api.errors import (
    ReabstractDocumentAlreadyInFlightError,
    RecomputePipelineAlreadyInFlightError,
)
from sage.config import VaultConfig
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import IngestRequest
from sage.services.ingestion import IngestionService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.storage.locks import DocumentLockManager
from tests.sage.test_ingestion import _create_test_file

_TERMINAL_STATES = {
    PipelineStatus.ABSTRACTION_COMPLETE,
    PipelineStatus.ABSTRACTION_SKIPPED,
    PipelineStatus.FAILED,
}


async def _await_terminal(graph_store, doc_id: str, *, attempts: int = 500) -> PipelineStatus:
    """Poll the document until it reaches a terminal pipeline_status."""
    for _ in range(attempts):
        doc = await graph_store.get_document(doc_id)
        if doc is not None and doc.pipeline_status in _TERMINAL_STATES:
            return doc.pipeline_status
        await asyncio.sleep(0.01)
    raise AssertionError(f"document {doc_id} did not reach terminal status in time")


# --------------------------------------------------------------------------
# Test-local abstraction providers. None of these acquire the module-level
# _generation_lock (that lives inside the real Qwen3 provider), so any
# serialization observed in these tests comes from the single worker draining
# the queue one job at a time — exactly the property under test.
# --------------------------------------------------------------------------


class _GatedAbstractionProvider(AbstractionProvider):
    """Blocks inside generate_abstract until released, so a job can be held
    in flight while a second caller is rejected by the in-flight claim."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.calls = 0

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.calls += 1
        self.entered.set()
        await self.gate.wait()
        return "gated abstract"


class _FailNTimesProvider(AbstractionProvider):
    """Fails the first ``n`` calls, then succeeds. Counts every call."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.calls = 0

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.calls += 1
        if self.calls <= self.n:
            raise RuntimeError(f"LLM unavailable (simulated failure {self.calls})")
        return "recovered abstract"


class _AlwaysFailProvider(AbstractionProvider):
    """Always raises. Counts every call so attempt bounds are observable."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.calls += 1
        raise RuntimeError("LLM unavailable (simulated failure)")


class _ConcurrencyTrackingProvider(AbstractionProvider):
    """Records the maximum number of concurrently-active generate_abstract
    calls. With a single serial worker this must never exceed 1."""

    def __init__(self) -> None:
        self.current = 0
        self.max_seen = 0

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.current += 1
        self.max_seen = max(self.max_seen, self.current)
        # Hold long enough that an overlapping second call would be visible.
        await asyncio.sleep(0.03)
        self.current -= 1
        return "concurrency abstract"


class _SpyAdapter:
    """Wraps a real source adapter and counts project() calls, so a test can
    assert recovery-from-chunks did NOT re-project from source."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.project_calls = 0

    async def project(self, path, config):
        self.project_calls += 1
        return await self._inner.project(path, config)


def _build_service(graph_store, config, provider, *, content_store=None) -> IngestionService:
    """Construct an IngestionService with a custom config/provider, mirroring
    the conftest ``ingestion_service`` fixture wiring (lifecycle_service is
    optional for a basic new-document ingest, per ingestion_service_no_abstraction)."""
    return IngestionService(
        graph_store=graph_store,
        lock_manager=DocumentLockManager(),
        content_store=content_store or StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=provider,
        config=config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )


async def _seed_indexed_doc(service, tmp_vault_dir, name: str, content: str | None = None) -> str:
    """Ingest a document fully via the inline (wait_for_pipeline=True) path so
    it lands at abstraction_complete with chunks. Returns the document id.
    Used as the starting point for recovery / reconciliation scenarios, which
    then mutate pipeline_status back to a non-terminal value. Content is made
    unique per name so distinct seeds do not collide on duplicate-content
    detection (BH-018)."""
    if content is None:
        content = f"# Doc {name}\n\nBody text for {name}."
    _create_test_file(tmp_vault_dir, name, content)
    request = IngestRequest(source=name, source_type=SourceType.MARKDOWN)
    result = await service.ingest(request, wait_for_pipeline=True)
    return result.document.id


@pytest.fixture(autouse=True)
async def _stop_fixture_worker(ingestion_service):
    """Stop the conftest ingestion_service's worker after each test so a
    lingering ``queue.get()`` task is not destroyed-while-pending at loop close."""
    yield
    await ingestion_service.stop_worker()


# ==========================================================================
# AC1 / AC2 — enqueue-and-return + single-worker serialization
# ==========================================================================


async def test_ingest_enqueues_and_returns_nonterminal(
    tmp_vault_dir, ingestion_service, graph_store
):
    """ingest(wait_for_pipeline=False) returns immediately with a non-terminal
    pipeline_status; the single worker drains the job to abstraction_complete."""
    _create_test_file(tmp_vault_dir, "samples/q1.md", "# Q1\n\nContent.")
    gated = _GatedAbstractionProvider()
    ingestion_service._abstraction = gated

    request = IngestRequest(source="samples/q1.md", source_type=SourceType.MARKDOWN)
    result = await ingestion_service.ingest(request, wait_for_pipeline=False)
    doc_id = result.document.id

    # Returned snapshot is non-terminal — work has been enqueued, not run.
    assert result.document.pipeline_status not in _TERMINAL_STATES

    # Exactly one long-lived worker task drains the queue.
    await asyncio.wait_for(gated.entered.wait(), timeout=2.0)
    assert ingestion_service._worker_task is not None
    assert not ingestion_service._worker_task.done()

    gated.gate.set()
    terminal = await _await_terminal(graph_store, doc_id)
    assert terminal == PipelineStatus.ABSTRACTION_COMPLETE


async def test_single_worker_serializes_concurrent_ingests(
    tmp_vault_dir, ingestion_service, graph_store
):
    """Two concurrent enqueues are drained one-at-a-time: the max observed
    abstraction concurrency is 1 (a parallel-task model would show 2)."""
    _create_test_file(tmp_vault_dir, "samples/s1.md", "# S1\n\nContent one.")
    _create_test_file(tmp_vault_dir, "samples/s2.md", "# S2\n\nContent two.")
    tracker = _ConcurrencyTrackingProvider()
    ingestion_service._abstraction = tracker

    r1 = await ingestion_service.ingest(
        IngestRequest(source="samples/s1.md", source_type=SourceType.MARKDOWN),
        wait_for_pipeline=False,
    )
    r2 = await ingestion_service.ingest(
        IngestRequest(source="samples/s2.md", source_type=SourceType.MARKDOWN),
        wait_for_pipeline=False,
    )

    assert await _await_terminal(graph_store, r1.document.id) == PipelineStatus.ABSTRACTION_COMPLETE
    assert await _await_terminal(graph_store, r2.document.id) == PipelineStatus.ABSTRACTION_COMPLETE
    assert tracker.max_seen == 1


# ==========================================================================
# AC3 — retry with bounded backoff, terminal structured failure
# ==========================================================================


async def test_abstraction_retried_then_succeeds(tmp_vault_dir, ingestion_service, graph_store):
    """Two transient failures then success: the doc reaches abstraction_complete
    and the provider was invoked exactly three times (1 + 2 retries)."""
    _create_test_file(tmp_vault_dir, "samples/r3.md", "# R3\n\nContent.")
    provider = _FailNTimesProvider(2)
    ingestion_service._abstraction = provider

    async def _no_sleep(_seconds):
        return None

    ingestion_service._sleep_for_backoff = _no_sleep

    result = await ingestion_service.ingest(
        IngestRequest(source="samples/r3.md", source_type=SourceType.MARKDOWN),
        wait_for_pipeline=False,
    )
    terminal = await _await_terminal(graph_store, result.document.id)

    assert terminal == PipelineStatus.ABSTRACTION_COMPLETE
    assert provider.calls == 3


async def test_terminal_failed_after_max_attempts(tmp_vault_dir, ingestion_service, graph_store):
    """A persistently-failing abstraction exhausts max_attempts (default 3),
    ends FAILED, and records a structured pipeline_error carrying the attempt
    count and the last error message."""
    _create_test_file(tmp_vault_dir, "samples/r4.md", "# R4\n\nContent.")
    provider = _AlwaysFailProvider()
    ingestion_service._abstraction = provider

    async def _no_sleep(_seconds):
        return None

    ingestion_service._sleep_for_backoff = _no_sleep

    result = await ingestion_service.ingest(
        IngestRequest(source="samples/r4.md", source_type=SourceType.MARKDOWN),
        wait_for_pipeline=False,
    )
    terminal = await _await_terminal(graph_store, result.document.id)

    assert terminal == PipelineStatus.FAILED
    assert provider.calls == 3  # max_attempts
    doc = await graph_store.get_document(result.document.id)
    assert "3 attempts" in doc.pipeline_error
    assert "LLM unavailable" in doc.pipeline_error


async def test_retry_backoff_bounded(tmp_vault_dir, minimal_vault_config_dict, graph_store):
    """Backoff delays follow min(base * 2**(k-1), max) and are capped — proving
    the bound, not merely that *some* delay occurs."""
    import copy

    cfg_dict = copy.deepcopy(minimal_vault_config_dict)
    cfg_dict["abstraction"] = {
        "max_attempts": 5,
        "retry_backoff_base_seconds": 1.0,
        "retry_backoff_max_seconds": 3.0,
    }
    config = VaultConfig.model_validate(cfg_dict)
    provider = _AlwaysFailProvider()
    service = _build_service(graph_store, config, provider)

    delays: list[float] = []

    async def _record(seconds):
        delays.append(seconds)

    service._sleep_for_backoff = _record
    try:
        _create_test_file(tmp_vault_dir, "samples/r5.md", "# R5\n\nContent.")
        result = await service.ingest(
            IngestRequest(source="samples/r5.md", source_type=SourceType.MARKDOWN),
            wait_for_pipeline=False,
        )
        assert await _await_terminal(graph_store, result.document.id) == PipelineStatus.FAILED
    finally:
        await service.stop_worker()

    # 5 attempts → 4 inter-attempt backoffs: k=1..4 → 1, 2, min(4,3)=3, min(8,3)=3.
    assert delays == [1.0, 2.0, 3.0, 3.0]


# ==========================================================================
# AC4 — no double-run vs reabstract / recompute (unified in-flight claim)
# ==========================================================================


async def test_reabstract_rejected_while_inflight(tmp_vault_dir, ingestion_service, graph_store):
    """While a document's abstraction job is claimed/in-flight, a concurrent
    reabstract(same_doc) is rejected with the claim's start_time."""
    doc_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/c1.md")
    gated = _GatedAbstractionProvider()
    ingestion_service._abstraction = gated

    await ingestion_service.reabstract(doc_id)
    await asyncio.wait_for(gated.entered.wait(), timeout=2.0)

    with pytest.raises(ReabstractDocumentAlreadyInFlightError):
        await ingestion_service.reabstract(doc_id)

    gated.gate.set()
    await _await_terminal(graph_store, doc_id)


async def test_recompute_rejected_while_inflight(tmp_vault_dir, ingestion_service, graph_store):
    """While a document's job is in-flight, a concurrent recompute_pipeline(
    same_doc) is rejected."""
    doc_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/c2.md")
    gated = _GatedAbstractionProvider()
    ingestion_service._abstraction = gated

    await ingestion_service.recompute_pipeline(doc_id)
    await asyncio.wait_for(gated.entered.wait(), timeout=2.0)

    with pytest.raises(RecomputePipelineAlreadyInFlightError):
        await ingestion_service.recompute_pipeline(doc_id)

    gated.gate.set()
    await _await_terminal(graph_store, doc_id)


async def test_one_abstraction_run_per_document(tmp_vault_dir, ingestion_service, graph_store):
    """A queued job plus a rejected concurrent reabstract yields exactly one
    abstraction invocation for the document — the core no-double-run invariant."""
    doc_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/c3.md")
    gated = _GatedAbstractionProvider()
    ingestion_service._abstraction = gated

    await ingestion_service.reabstract(doc_id)
    await asyncio.wait_for(gated.entered.wait(), timeout=2.0)

    with pytest.raises(ReabstractDocumentAlreadyInFlightError):
        await ingestion_service.reabstract(doc_id)

    gated.gate.set()
    await _await_terminal(graph_store, doc_id)
    assert gated.calls == 1


# ==========================================================================
# AC5 / AC6 — startup recovery re-derived from pipeline_status
# ==========================================================================


async def test_recover_projection_complete_document(tmp_vault_dir, ingestion_service, graph_store):
    """A document stranded at projection_complete with no chunks is recovered:
    the worker re-projects from source, runs Stage 2-3, and lands chunks +
    abstraction_complete."""
    doc_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/p1.md")
    # Strand it: drop chunks, reset to projection_complete with no abstract.
    await ingestion_service._content_store.remove_document(doc_id)
    await graph_store.update_document(
        doc_id,
        {"pipeline_status": PipelineStatus.PROJECTION_COMPLETE.value, "semantic_abstract": None},
    )

    count = await ingestion_service.recover_incomplete_documents()
    assert count == 1

    terminal = await _await_terminal(graph_store, doc_id)
    assert terminal == PipelineStatus.ABSTRACTION_COMPLETE
    assert await ingestion_service._content_store.has_chunks(doc_id)


async def test_recover_indexed_document_from_chunks(tmp_vault_dir, ingestion_service, graph_store):
    """A document at indexing_complete WITH chunks is recovered via Stage 3
    only — the source adapter is NOT re-invoked (chunk reuse, not re-projection)."""
    doc_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/p2.md")
    await graph_store.update_document(
        doc_id,
        {"pipeline_status": PipelineStatus.INDEXING_COMPLETE.value, "semantic_abstract": None},
    )
    spy = _SpyAdapter(MarkdownAdapter())
    ingestion_service._adapters[SourceType.MARKDOWN] = spy

    count = await ingestion_service.recover_incomplete_documents()
    assert count == 1

    terminal = await _await_terminal(graph_store, doc_id)
    assert terminal == PipelineStatus.ABSTRACTION_COMPLETE
    assert spy.project_calls == 0


async def test_recover_skips_terminal_documents(tmp_vault_dir, ingestion_service, graph_store):
    """Documents in terminal states (abstraction_complete / abstraction_skipped
    / failed) are not recovered: recovery enqueues nothing."""
    complete_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/t1.md")
    skipped_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/t2.md")
    failed_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/t3.md")
    await graph_store.update_document(
        skipped_id, {"pipeline_status": PipelineStatus.ABSTRACTION_SKIPPED.value}
    )
    await graph_store.update_document(
        failed_id, {"pipeline_status": PipelineStatus.FAILED.value, "pipeline_error": "old"}
    )

    provider = _AlwaysFailProvider()
    ingestion_service._abstraction = provider

    count = await ingestion_service.recover_incomplete_documents()
    assert count == 0
    # Give any (erroneously) enqueued work a chance to run before asserting.
    await asyncio.sleep(0.05)
    assert provider.calls == 0
    # Terminal documents are untouched.
    for did, expected in (
        (complete_id, PipelineStatus.ABSTRACTION_COMPLETE),
        (skipped_id, PipelineStatus.ABSTRACTION_SKIPPED),
        (failed_id, PipelineStatus.FAILED),
    ):
        doc = await graph_store.get_document(did)
        assert doc.pipeline_status == expected


async def test_recover_returns_enqueued_count(tmp_vault_dir, ingestion_service, graph_store):
    """recover_incomplete_documents() returns the number of non-terminal
    documents it enqueued, across a mixed seed."""
    keep_terminal = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/m0.md")
    assert keep_terminal  # abstraction_complete, must be excluded
    nonterminal_ids = []
    for name, status in (
        ("samples/m1.md", PipelineStatus.PROJECTION_COMPLETE),
        ("samples/m2.md", PipelineStatus.INDEXING_COMPLETE),
        ("samples/m3.md", PipelineStatus.ABSTRACTION_IN_PROGRESS),
    ):
        did = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, name)
        await graph_store.update_document(did, {"pipeline_status": status.value})
        nonterminal_ids.append(did)

    count = await ingestion_service.recover_incomplete_documents()
    assert count == len(nonterminal_ids)


# ==========================================================================
# Lifecycle + inline-path regression
# ==========================================================================


async def test_stop_worker_cancels_cleanly(tmp_vault_dir, ingestion_service, graph_store):
    """stop_worker() cancels and awaits the drain task without raising; a later
    enqueue restarts it."""
    _create_test_file(tmp_vault_dir, "samples/w1.md", "# W1\n\nContent.")
    r1 = await ingestion_service.ingest(
        IngestRequest(source="samples/w1.md", source_type=SourceType.MARKDOWN),
        wait_for_pipeline=False,
    )
    assert await _await_terminal(graph_store, r1.document.id) == PipelineStatus.ABSTRACTION_COMPLETE

    await ingestion_service.stop_worker()
    assert ingestion_service._worker_task is None or ingestion_service._worker_task.done()

    # A later enqueue restarts the worker and drains.
    _create_test_file(tmp_vault_dir, "samples/w2.md", "# W2\n\nContent.")
    r2 = await ingestion_service.ingest(
        IngestRequest(source="samples/w2.md", source_type=SourceType.MARKDOWN),
        wait_for_pipeline=False,
    )
    assert await _await_terminal(graph_store, r2.document.id) == PipelineStatus.ABSTRACTION_COMPLETE


async def test_wait_for_pipeline_true_runs_inline(tmp_vault_dir, minimal_config, graph_store):
    """The inline (wait_for_pipeline=True) path is not routed through the queue:
    it returns a terminal status synchronously, and a failing provider yields
    FAILED after a SINGLE attempt (no retry on the inline path)."""
    provider = _AlwaysFailProvider()
    service = _build_service(graph_store, minimal_config, provider)
    try:
        _create_test_file(tmp_vault_dir, "samples/i1.md", "# I1\n\nContent.")
        result = await service.ingest(
            IngestRequest(source="samples/i1.md", source_type=SourceType.MARKDOWN),
            wait_for_pipeline=True,
        )
        # Synchronous terminal status on return — not enqueued.
        assert result.document.pipeline_status == PipelineStatus.FAILED
        assert provider.calls == 1  # fail-fast: no retry inline
    finally:
        await service.stop_worker()
