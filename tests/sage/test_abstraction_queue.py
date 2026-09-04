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

from sage.adapters.interfaces import (
    AbstractionInputTooLargeError,
    AbstractionMemoryExhaustedError,
    AbstractionProvider,
)
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


class _MarkedFailProvider(AbstractionProvider):
    """Always raises, carrying a caller-supplied marker in the message.

    Two instances with distinct markers make it observable whether a second
    failure replaced the first document's ``pipeline_error`` or appended to it.
    """

    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.calls = 0

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.calls += 1
        raise RuntimeError(self.marker)


class _NonRetryableFailProvider(AbstractionProvider):
    """Always raises a failure that is deterministic in its input. Counts every
    call so the absence of retries is observable."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.calls += 1
        raise AbstractionInputTooLargeError("test-model", 431_204, 199_000)


class _MemoryExhaustedFailProvider(AbstractionProvider):
    """Always raises the accelerator-memory exhaustion failure. Counts every
    call so the absence of retries is observable."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.calls += 1
        raise AbstractionMemoryExhaustedError("test-model", 512_000)


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


async def test_reabstract_clears_stale_pipeline_error(
    tmp_vault_dir, ingestion_service, graph_store
):
    """A document repaired by reabstract does not keep the error it recovered from.

    ``reabstract`` regenerates the abstract from stored chunks without
    re-projecting, so it is the one repair path that reaches a successful
    terminal status without passing a Stage-1 write. The document must reach
    abstraction_complete with pipeline_error null.

    Anti-coincidental-pass: a document that never carried a pipeline_error
    ends at null for free, so the failure is asserted first -- the field is
    proved non-null before the repair that must clear it.
    """
    _create_test_file(tmp_vault_dir, "samples/stale.md", "# Stale\n\nContent.")
    # Fails the 3 attempts the ingest budget allows, then succeeds on the
    # 4th call, which is the one the reabstract job makes.
    provider = _FailNTimesProvider(3)
    ingestion_service._abstraction = provider

    async def _no_sleep(_seconds):
        return None

    ingestion_service._sleep_for_backoff = _no_sleep

    result = await ingestion_service.ingest(
        IngestRequest(source="samples/stale.md", source_type=SourceType.MARKDOWN),
        wait_for_pipeline=False,
    )
    assert await _await_terminal(graph_store, result.document.id) == PipelineStatus.FAILED
    failed = await graph_store.get_document(result.document.id)
    assert "3 attempts" in failed.pipeline_error

    await ingestion_service.reabstract(result.document.id)
    terminal = await _await_terminal(graph_store, result.document.id)

    assert terminal == PipelineStatus.ABSTRACTION_COMPLETE
    repaired = await graph_store.get_document(result.document.id)
    assert repaired.semantic_abstract == "recovered abstract"
    assert repaired.pipeline_error is None


async def test_repeat_failure_replaces_prior_pipeline_error(
    tmp_vault_dir, ingestion_service, graph_store
):
    """A second failure replaces the recorded error rather than appending to it.

    Anti-coincidental-pass: asserting only that the newer marker is present
    passes just as well under an append. The absence of the older marker is
    what distinguishes replace from append.
    """
    _create_test_file(tmp_vault_dir, "samples/twice.md", "# Twice\n\nContent.")
    ingestion_service._abstraction = _MarkedFailProvider("simulated failure A")

    async def _no_sleep(_seconds):
        return None

    ingestion_service._sleep_for_backoff = _no_sleep

    result = await ingestion_service.ingest(
        IngestRequest(source="samples/twice.md", source_type=SourceType.MARKDOWN),
        wait_for_pipeline=False,
    )
    assert await _await_terminal(graph_store, result.document.id) == PipelineStatus.FAILED
    first = await graph_store.get_document(result.document.id)
    assert "simulated failure A" in first.pipeline_error

    ingestion_service._abstraction = _MarkedFailProvider("simulated failure B")
    await ingestion_service.reabstract(result.document.id)
    terminal = await _await_terminal(graph_store, result.document.id)

    assert terminal == PipelineStatus.FAILED
    second = await graph_store.get_document(result.document.id)
    assert "simulated failure B" in second.pipeline_error
    assert "simulated failure A" not in second.pipeline_error


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


async def test_recover_re_enqueues_an_interrupted_document(
    tmp_vault_dir, ingestion_service, graph_store
):
    """abstraction_interrupted is the one terminal status recovery re-runs.

    The seed carries the other three terminal statuses alongside it, and the
    count is asserted exactly, so the test cannot pass on a stray non-terminal
    document: it pins that recovery reaches this status and none of its
    terminal siblings.
    """
    seeded = {}
    for name, status in (
        ("samples/r1.md", PipelineStatus.ABSTRACTION_INTERRUPTED),
        ("samples/r2.md", PipelineStatus.ABSTRACTION_SKIPPED),
        ("samples/r3.md", PipelineStatus.ABSTRACTION_COMPLETE),
        ("samples/r4.md", PipelineStatus.FAILED),
    ):
        did = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, name)
        await graph_store.update_document(did, {"pipeline_status": status.value})
        seeded[status] = did

    count = await ingestion_service.recover_incomplete_documents()
    assert count == 1

    interrupted = seeded[PipelineStatus.ABSTRACTION_INTERRUPTED]
    assert await _await_terminal(graph_store, interrupted) == PipelineStatus.ABSTRACTION_COMPLETE
    for status in (
        PipelineStatus.ABSTRACTION_SKIPPED,
        PipelineStatus.ABSTRACTION_COMPLETE,
        PipelineStatus.FAILED,
    ):
        doc = await graph_store.get_document(seeded[status])
        assert doc.pipeline_status == status


# ==========================================================================
# Worker stop settles the work it drops
# ==========================================================================


async def test_stop_worker_restamps_a_queued_job_document(
    tmp_vault_dir, ingestion_service, graph_store
):
    """A job still waiting in the queue when the worker stops is settled at
    abstraction_interrupted rather than left non-terminal forever.

    The gated provider is what makes the precondition real: it holds the first
    document inside generate_abstract so the second is provably still queued,
    and ``calls == 1`` afterwards proves the second never reached the provider.
    Without that, a document that had simply finished would satisfy the same
    final assertion.
    """
    held_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/s1.md")
    queued_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/s2.md")
    gated = _GatedAbstractionProvider()
    ingestion_service._abstraction = gated

    await ingestion_service.reabstract(held_id)
    await asyncio.wait_for(gated.entered.wait(), timeout=2.0)
    await ingestion_service.reabstract(queued_id)

    queued = await graph_store.get_document(queued_id)
    assert queued.pipeline_status == PipelineStatus.ABSTRACTION_IN_PROGRESS
    assert queued_id in ingestion_service._inflight

    await ingestion_service.stop_worker()

    queued = await graph_store.get_document(queued_id)
    assert queued.pipeline_status == PipelineStatus.ABSTRACTION_INTERRUPTED
    assert queued.pipeline_error
    assert queued_id not in ingestion_service._inflight
    assert gated.calls == 1  # the queued job never reached the provider


async def test_stop_worker_restamps_the_in_flight_document(
    tmp_vault_dir, ingestion_service, graph_store
):
    """The document cancelled mid-generation is settled too, not only the ones
    that never started. Its claim is released by its own unwinding, which is
    why stop_worker snapshots the claims before it cancels."""
    doc_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/s3.md")
    gated = _GatedAbstractionProvider()
    ingestion_service._abstraction = gated

    await ingestion_service.reabstract(doc_id)
    await asyncio.wait_for(gated.entered.wait(), timeout=2.0)
    assert doc_id in ingestion_service._inflight

    await ingestion_service.stop_worker()

    doc = await graph_store.get_document(doc_id)
    assert doc.pipeline_status == PipelineStatus.ABSTRACTION_INTERRUPTED
    assert doc_id not in ingestion_service._inflight


async def test_stop_worker_leaves_a_terminal_document_alone(
    tmp_vault_dir, ingestion_service, graph_store
):
    """A document that reached a real outcome is not overwritten by the
    interruption stamp.

    This is the guard's own test: the claim snapshot names every document the
    worker was carrying, and one of them may have settled between the snapshot
    and the cancellation. Against an unconditional stamp this test fails,
    reporting work that did happen as work that did not.

    Anti-coincidental-pass: the two outcomes are asserted as a pair. The
    settled document alone leaves the held document's setup inert -- delete
    the held document and the test still passes -- so the second assertion is
    what puts that setup in an assertion's causal path.

    What the pair does NOT establish is that a terminal document cannot end
    the pass for the rest of the batch. The settle pass iterates in sorted id
    order and the ids are content-derived, so an implementation that stopped
    at the first terminal document would still stamp the held one whenever it
    sorted first. The order-independent version of that rival is the store
    error, pinned in test_stop_worker_survives_a_restamp_write_failure by
    poisoning whichever document comes first rather than a named one.
    """
    settled_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/s4.md")
    held_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/s5.md")
    gated = _GatedAbstractionProvider()
    ingestion_service._abstraction = gated

    # Claim the settled document by hand and leave the claim in place, so it is
    # named by the snapshot exactly as a document whose job had just finished.
    ingestion_service._try_claim(settled_id, "test")
    await ingestion_service.reabstract(held_id)
    await asyncio.wait_for(gated.entered.wait(), timeout=2.0)

    await ingestion_service.stop_worker()

    settled = await graph_store.get_document(settled_id)
    assert settled.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE
    assert settled.pipeline_error is None

    held = await graph_store.get_document(held_id)
    assert held.pipeline_status == PipelineStatus.ABSTRACTION_INTERRUPTED


async def test_stop_worker_restamp_opt_out_leaves_the_document_non_terminal(
    tmp_vault_dir, minimal_config, graph_store
):
    """restamp=False skips the stamp, for the caller whose documents are about
    to cease to exist.

    Both arms run the same setup, so the opt-out arm cannot pass merely by
    having had no pending work to settle.
    """
    results = {}
    for name, restamp in (("samples/s6.md", False), ("samples/s7.md", True)):
        gated = _GatedAbstractionProvider()
        service = _build_service(graph_store, minimal_config, _FailNTimesProvider(0))
        try:
            doc_id = await _seed_indexed_doc(service, tmp_vault_dir, name)
            # Swapped in only after the seed: the seed ingest runs inline and
            # would block on the gate.
            service._abstraction = gated
            await service.reabstract(doc_id)
            await asyncio.wait_for(gated.entered.wait(), timeout=2.0)
            await service.stop_worker(restamp=restamp)
        finally:
            gated.gate.set()
        results[restamp] = (await graph_store.get_document(doc_id)).pipeline_status

    assert results[False] == PipelineStatus.ABSTRACTION_IN_PROGRESS
    assert results[True] == PipelineStatus.ABSTRACTION_INTERRUPTED


async def test_stop_worker_releases_the_claim_of_a_dropped_queued_job(
    tmp_vault_dir, ingestion_service, graph_store
):
    """A queued job never enters the function whose finally releases its claim,
    so the stop must release it. Left in place, the claim outlives the work it
    guarded and rejects the next operation on that document."""
    held_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/s8.md")
    queued_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/s9.md")
    gated = _GatedAbstractionProvider()
    ingestion_service._abstraction = gated

    await ingestion_service.reabstract(held_id)
    await asyncio.wait_for(gated.entered.wait(), timeout=2.0)
    await ingestion_service.reabstract(queued_id)
    assert queued_id in ingestion_service._inflight

    await ingestion_service.stop_worker()

    # Not merely absent from the registry: the next operation is accepted.
    ingestion_service._abstraction = _FailNTimesProvider(0)
    result = await ingestion_service.reabstract(queued_id)
    assert result["status"] == "reabstract_started"
    assert await _await_terminal(graph_store, queued_id) == PipelineStatus.ABSTRACTION_COMPLETE


async def test_stop_worker_with_no_pending_work_writes_no_document(
    tmp_vault_dir, ingestion_service, graph_store
):
    """An idle stop touches the store not at all.

    stop_worker runs from an autouse test teardown and from every production
    teardown, each immediately before the storage handle is released. A stop
    that queried on every call would put a round-trip there.

    Reads are counted alongside writes, because the round-trip is the cost
    being guarded and a settle pass that reads every document to discover it
    has nothing to write pays it in full. Asserting on writes alone would
    pass against that implementation.
    """
    doc_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/s10.md")
    assert doc_id
    assert not ingestion_service._inflight

    touched = []
    original_write = graph_store.update_document
    original_read = graph_store.get_document

    async def _recording_write(document_id, updates):
        touched.append(("write", document_id))
        return await original_write(document_id, updates)

    async def _recording_read(document_id):
        touched.append(("read", document_id))
        return await original_read(document_id)

    graph_store.update_document = _recording_write
    graph_store.get_document = _recording_read
    try:
        await ingestion_service.stop_worker()
    finally:
        graph_store.update_document = original_write
        graph_store.get_document = original_read

    assert touched == []


async def test_stop_worker_survives_a_restamp_write_failure(
    tmp_vault_dir, ingestion_service, graph_store
):
    """A store error while settling strands only the document it belongs to.

    Every caller runs the stop immediately before closing the timing flusher
    and releasing the storage pool, so an exception escaping here would skip
    both and leak the pool it was about to close.

    Anti-coincidental-pass: returning normally is not enough on its own, and
    neither is a single document's outcome. Two documents are settled and only
    the FIRST read raises, so exactly one must still reach
    abstraction_interrupted -- which separates per-document isolation from a
    pass that gives up at the first error and strands the rest.

    The failure is positional rather than keyed to a document id, and that is
    what makes the assertion independent of batch order. The settle pass
    iterates its snapshot in sorted id order, and the ids are content-derived,
    so a test that poisons a *named* document passes against the
    give-up-at-the-first-error implementation whenever the healthy document
    happens to sort first. Poisoning whichever document comes first leaves
    that implementation no ordering to be rescued by.

    Both claims are asserted too, and the queued document is what makes that
    discriminate: the in-flight one releases its own claim while unwinding, so
    it reports released whether or not the stop did anything, while a job that
    never ran depends on the stop to release it.
    """
    held_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/s11.md")
    queued_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/s12.md")
    gated = _GatedAbstractionProvider()
    ingestion_service._abstraction = gated

    await ingestion_service.reabstract(held_id)
    await asyncio.wait_for(gated.entered.wait(), timeout=2.0)
    await ingestion_service.reabstract(queued_id)
    assert queued_id in ingestion_service._inflight

    original = graph_store.get_document
    reads = []

    async def _raising_on_first(document_id):
        reads.append(document_id)
        if len(reads) == 1:
            raise RuntimeError("storage is going away")
        return await original(document_id)

    graph_store.get_document = _raising_on_first
    try:
        await ingestion_service.stop_worker()
    finally:
        graph_store.get_document = original

    statuses = [
        (await graph_store.get_document(did)).pipeline_status for did in (held_id, queued_id)
    ]
    assert statuses.count(PipelineStatus.ABSTRACTION_INTERRUPTED) == 1
    assert statuses.count(PipelineStatus.ABSTRACTION_IN_PROGRESS) == 1

    assert held_id not in ingestion_service._inflight
    assert queued_id not in ingestion_service._inflight


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


async def test_non_retryable_failure_terminates_on_first_attempt(
    tmp_vault_dir, ingestion_service, graph_store
):
    """A failure deterministic in the document ends the job on its first
    attempt: no second call, no backoff, and a pipeline_error that reports the
    one attempt made rather than a budget it never spent.

    Anti-coincidental-pass: the terminal status alone proves nothing here --
    ``test_terminal_failed_after_max_attempts`` already reaches FAILED for an
    ordinary failure, so a status assertion passes with the classification
    removed entirely. The call count and the untouched backoff seam are the
    only assertions that distinguish declining to retry from retrying and
    failing.
    """
    _create_test_file(tmp_vault_dir, "samples/nr1.md", "# NR1\n\nContent.")
    provider = _NonRetryableFailProvider()
    ingestion_service._abstraction = provider

    delays: list[float] = []

    async def _record(seconds):
        delays.append(seconds)

    ingestion_service._sleep_for_backoff = _record

    result = await ingestion_service.ingest(
        IngestRequest(source="samples/nr1.md", source_type=SourceType.MARKDOWN),
        wait_for_pipeline=False,
    )
    terminal = await _await_terminal(graph_store, result.document.id)

    assert terminal == PipelineStatus.FAILED
    assert provider.calls == 1  # max_attempts is 3; the budget is not spent
    assert delays == []
    doc = await graph_store.get_document(result.document.id)
    assert "not retried" in doc.pipeline_error
    assert "AbstractionInputTooLargeError" in doc.pipeline_error
    assert "attempts" not in doc.pipeline_error


async def test_ordinary_failure_still_exhausts_the_retry_budget(
    tmp_vault_dir, ingestion_service, graph_store
):
    """An ordinary failure keeps the full retry budget and backs off between
    attempts, unchanged by the non-retryable classification.

    Anti-coincidental-pass: the regression guard for over-broad
    classification. Treating every exception as deterministic passes the
    non-retryable test above and silently strips retries from transient
    failures -- throttling, a briefly unreachable model -- which only this
    control catches.
    """
    _create_test_file(tmp_vault_dir, "samples/nr2.md", "# NR2\n\nContent.")
    provider = _AlwaysFailProvider()
    ingestion_service._abstraction = provider

    delays: list[float] = []

    async def _record(seconds):
        delays.append(seconds)

    ingestion_service._sleep_for_backoff = _record

    result = await ingestion_service.ingest(
        IngestRequest(source="samples/nr2.md", source_type=SourceType.MARKDOWN),
        wait_for_pipeline=False,
    )
    terminal = await _await_terminal(graph_store, result.document.id)

    assert terminal == PipelineStatus.FAILED
    assert provider.calls == 3  # max_attempts
    assert len(delays) == 2  # one backoff between each pair of attempts
    doc = await graph_store.get_document(result.document.id)
    assert "3 attempts" in doc.pipeline_error


async def test_memory_exhaustion_terminates_on_first_attempt(
    tmp_vault_dir, ingestion_service, graph_store
):
    """Accelerator-memory exhaustion during generation ends the job on its
    first attempt: no second call, no backoff. Each avoided retry is a full
    prefill the worker would otherwise re-pay before reaching the same
    allocation failure.

    Anti-coincidental-pass: the worker's classification arm is generic over
    the non-retryable base class, so what this test pins is the subclass
    relation itself. Were the memory-exhaustion type to stop subclassing it,
    FAILED would still be reached after the full budget -- only the call
    count and the empty backoff seam go red then.
    """
    _create_test_file(tmp_vault_dir, "samples/nr3.md", "# NR3\n\nContent.")
    provider = _MemoryExhaustedFailProvider()
    ingestion_service._abstraction = provider

    delays: list[float] = []

    async def _record(seconds):
        delays.append(seconds)

    ingestion_service._sleep_for_backoff = _record

    result = await ingestion_service.ingest(
        IngestRequest(source="samples/nr3.md", source_type=SourceType.MARKDOWN),
        wait_for_pipeline=False,
    )
    terminal = await _await_terminal(graph_store, result.document.id)

    assert terminal == PipelineStatus.FAILED
    assert provider.calls == 1  # max_attempts is 3; the budget is not spent
    assert delays == []
    doc = await graph_store.get_document(result.document.id)
    assert "not retried" in doc.pipeline_error
    assert "AbstractionMemoryExhaustedError" in doc.pipeline_error
