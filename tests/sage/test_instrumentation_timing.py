"""Structured query-timing instrumentation across SAGE layers.

Each test holds the line on one acceptance criterion from the plan:

1. test_storage_emits_record_for_get_document — happy path: storage layer.
2. test_content_emits_record_for_search_semantic — happy path: content layer.
3. test_retrieval_emits_request_record_with_phases — happy path: retrieval.
4. test_warn_threshold_drives_warning_level — slow-query WARN path.
5. test_fast_path_suppresses_and_summary_aggregates — fast-path + flush.
6. test_null_query_timer_default_is_silent — no-op default safety.
7. test_uninstrumented_method_emits_nothing — write paths stay out of scope.
8. test_vault_config_back_compat_no_timing_block — Pydantic default round-trip.
9. test_mcp_init_attaches_per_vault_file_handler — wiring confirmation.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.adapters.content_store_lancedb import LanceDBContentStore
from sage.config import VaultConfig
from sage.instrumentation import (
    NULL_QUERY_TIMER,
    QueryTimer,
    TimingConfig,
    VaultTimingThread,
)
from sage.models.enums import (
    PipelineStatus,
    RetrievalMode,
    SourceType,
)
from sage.models.schemas import (
    DiscoverRequest,
    Document,
)
from sage.services.retrieval import RetrievalService
from sage.storage.graph_store import GraphStore


@pytest.fixture(autouse=True)
def _isolate_timing_loggers():
    """Reset the three timing loggers before AND after each test.

    Other suite-wide tests (notably the MCP lifespan tests) call
    initialize_services, which attaches a per-vault FileHandler and sets
    propagate=False on these loggers. Without isolation, caplog can't
    capture records on subsequent tests because the propagation path is
    broken. We also tear down to be polite to whatever runs next.
    """
    names = ("sage.storage.timing", "sage.content.timing", "sage.retrieval.timing")
    for name in names:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.propagate = True
        logger.setLevel(logging.NOTSET)
    yield
    for name in names:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.propagate = True
        logger.setLevel(logging.NOTSET)


def _payloads(caplog, logger_name: str) -> list[dict]:
    """Return JSON-decoded payloads from records on the given logger."""
    return [json.loads(rec.getMessage()) for rec in caplog.records if rec.name == logger_name]


def _doc(doc_id: str = "deadbeef_t73") -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title="t73",
        source_type=SourceType.MARKDOWN,
        source_path=f"t73/{doc_id}.md",
        lifecycle_status="active",
        source_content_hash="sha256:" + ("a" * 64),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
        tags=[],
    )


# ── 1. storage happy path ────────────────────────────────────────────


async def test_storage_emits_record_for_get_document(tmp_vault_dir, caplog):
    """Real GraphStore + real QueryTimer → one DEBUG record with the right JSON."""
    timer = QueryTimer(
        logger_name="sage.storage.timing",
        config=TimingConfig(emit_threshold_ms=0.0),
        layer="storage",
    )
    store = GraphStore(tmp_vault_dir / "brain" / "graph.db", query_timer=timer)
    await store.initialize()
    try:
        await store.insert_document(_doc())
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="sage.storage.timing"):
            await store.get_document("deadbeef_t73")

        payloads = _payloads(caplog, "sage.storage.timing")
        get_doc_payloads = [p for p in payloads if p.get("label") == "get_document"]
        assert len(get_doc_payloads) == 1, payloads
        payload = get_doc_payloads[0]
        assert payload["layer"] == "storage"
        assert isinstance(payload["duration_ms"], float)
        assert payload["duration_ms"] >= 0.0

        records_for_label = [
            r
            for r in caplog.records
            if r.name == "sage.storage.timing"
            and json.loads(r.getMessage()).get("label") == "get_document"
        ]
        assert records_for_label[0].levelno == logging.DEBUG
    finally:
        await store.close()


# ── 2. content happy path ────────────────────────────────────────────


async def test_content_emits_record_for_search_semantic(tmp_path, caplog):
    """Real LanceDBContentStore + real QueryTimer → record with layer=content."""
    brain = tmp_path / "brain"
    brain.mkdir()
    timer = QueryTimer(
        logger_name="sage.content.timing",
        config=TimingConfig(emit_threshold_ms=0.0),
        layer="content",
    )
    store = LanceDBContentStore(brain, query_timer=timer)

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="sage.content.timing"):
        # Search on an empty store returns [] but still flows through measure().
        await store.search_semantic([0.0] * 768, limit=5)

    payloads = _payloads(caplog, "sage.content.timing")
    search_payloads = [p for p in payloads if p.get("label") == "search_semantic"]
    assert len(search_payloads) == 1, payloads
    payload = search_payloads[0]
    assert payload["layer"] == "content"
    assert isinstance(payload["duration_ms"], float)
    assert payload["duration_ms"] >= 0.0


# ── 3. retrieval happy path with phases ──────────────────────────────


async def test_retrieval_emits_request_record_with_phases(
    graph_store, stub_content_store, stub_embedding_provider, minimal_config, caplog
):
    """RetrievalService.discover(mode=CATALOG) emits one request record with phases."""
    timer = QueryTimer(
        logger_name="sage.retrieval.timing",
        config=TimingConfig(emit_threshold_ms=0.0),
        layer="retrieval",
    )
    service = RetrievalService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
        query_timer=timer,
    )
    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        limit=5,
        offset=0,
    )

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="sage.retrieval.timing"):
        await service.discover(request)

    payloads = _payloads(caplog, "sage.retrieval.timing")
    request_payloads = [p for p in payloads if p.get("label", "").startswith("request:")]
    assert len(request_payloads) == 1, payloads
    payload = request_payloads[0]
    assert payload["layer"] == "retrieval"
    assert payload["mode"] == RetrievalMode.CATALOG.value
    assert isinstance(payload["phases"], dict)
    assert len(payload["phases"]) >= 1, "expected at least one named phase"
    for phase_name, phase_ms in payload["phases"].items():
        assert isinstance(phase_name, str) and phase_name
        assert isinstance(phase_ms, float)
        assert phase_ms >= 0.0


# ── 4. WARN threshold drives WARNING level ───────────────────────────


async def test_warn_threshold_drives_warning_level(tmp_vault_dir, caplog):
    """warn_threshold_ms=0 forces WARN on every emission; no sleep required."""
    timer = QueryTimer(
        logger_name="sage.storage.timing",
        config=TimingConfig(
            emit_threshold_ms=0.0,
            warn_threshold_ms=0.0,
        ),
        layer="storage",
    )
    store = GraphStore(tmp_vault_dir / "brain" / "graph.db", query_timer=timer)
    await store.initialize()
    try:
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="sage.storage.timing"):
            await store.get_document("nonexistent")

        warnings = [
            r
            for r in caplog.records
            if r.name == "sage.storage.timing" and r.levelno == logging.WARNING
        ]
        assert len(warnings) >= 1, [(r.levelno, r.getMessage()) for r in caplog.records]
    finally:
        await store.close()


# ── 5. fast-path suppression + summary aggregation ───────────────────


async def test_fast_path_suppresses_and_summary_aggregates(tmp_vault_dir, caplog):
    """Sub-threshold queries are counted, not emitted; flush() emits one summary."""
    timer = QueryTimer(
        logger_name="sage.storage.timing",
        config=TimingConfig(
            emit_threshold_ms=10_000.0,
            summary_interval_seconds=60.0,
        ),
        layer="storage",
    )
    store = GraphStore(tmp_vault_dir / "brain" / "graph.db", query_timer=timer)
    await store.initialize()
    try:
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="sage.storage.timing"):
            n = 5
            for _ in range(n):
                await store.get_document("nonexistent")

            payloads_pre = _payloads(caplog, "sage.storage.timing")
            assert all(p.get("label") != "get_document" for p in payloads_pre), (
                "fast-path queries should not emit per-call records"
            )

            timer.flush()

        payloads_post = _payloads(caplog, "sage.storage.timing")
        summaries = [p for p in payloads_post if p.get("summary") is True]
        assert len(summaries) == 1, payloads_post
        summary = summaries[0]
        assert summary["suppressed"].get("get_document") == n
        assert summary["layer"] == "storage"
        assert isinstance(summary["interval_s"], float)
    finally:
        await store.close()


# ── 6. no-op default is silent ───────────────────────────────────────


async def test_null_query_timer_default_is_silent(tmp_vault_dir, caplog):
    """Default kwarg = NULL_QUERY_TIMER: real query emits zero records."""
    store = GraphStore(tmp_vault_dir / "brain" / "graph.db")
    await store.initialize()
    try:
        caplog.clear()
        with caplog.at_level(
            logging.DEBUG,
            logger="sage.storage.timing",
        ):
            await store.get_document("nonexistent")

        records = [r for r in caplog.records if r.name.endswith(".timing")]
        assert records == [], records
    finally:
        await store.close()


# ── 7. uninstrumented method emits nothing ───────────────────────────


async def test_uninstrumented_method_emits_nothing(tmp_vault_dir, caplog):
    """Writes are out of scope; insert_document must not emit on the timing logger."""
    timer = QueryTimer(
        logger_name="sage.storage.timing",
        config=TimingConfig(emit_threshold_ms=0.0),
        layer="storage",
    )
    store = GraphStore(tmp_vault_dir / "brain" / "graph.db", query_timer=timer)
    await store.initialize()
    try:
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="sage.storage.timing"):
            await store.insert_document(_doc())

        write_labels = {
            "insert_document",
            "update_document",
            "delete_edge",
            "insert_edge",
        }
        offending = [
            p for p in _payloads(caplog, "sage.storage.timing") if p.get("label") in write_labels
        ]
        assert offending == [], offending
    finally:
        await store.close()


# ── 8. vault config back-compat (no timing block) ────────────────────


def test_vault_config_back_compat_no_timing_block(minimal_vault_config_dict):
    """A vault_config dict with no 'timing' key still loads with defaults."""
    assert "timing" not in minimal_vault_config_dict
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    assert config.timing is not None
    assert config.timing.enabled is True
    assert config.timing.emit_threshold_ms == 1.0
    assert config.timing.warn_threshold_ms == 100.0
    assert config.timing.summary_interval_seconds == 60.0
    assert config.timing.log_path is None


# ── 9. mcp_init attaches a per-vault file handler ────────────────────


async def test_mcp_init_attaches_per_vault_file_handler(minimal_vault_config_dict, monkeypatch):
    """initialize_services attaches a per-vault FileHandler to the three timing loggers."""
    from sage.mcp_init import initialize_services

    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")

    # Drop any leftover handlers from prior tests so the assertion is precise.
    for name in ("sage.storage.timing", "sage.content.timing", "sage.retrieval.timing"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)

    config = VaultConfig.model_validate(minimal_vault_config_dict)
    # Construct the real LanceDBContentStore via mcp_init's production path
    # so we can verify the content_timer is threaded through. Skip
    # gracefully if lancedb isn't available in this environment.
    try:
        import lancedb  # noqa: F401
    except ImportError:
        pytest.skip("lancedb not installed; mcp_init wiring test skipped")

    services = await initialize_services(config)
    try:
        # All three loggers got a handler pointing at brain_root/timing.log
        brain_root = Path(config.vault.brain_root)
        timing_log = brain_root / "timing.log"
        for name in (
            "sage.storage.timing",
            "sage.content.timing",
            "sage.retrieval.timing",
        ):
            logger = logging.getLogger(name)
            file_handlers = [
                h
                for h in logger.handlers
                if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == timing_log
            ]
            assert len(file_handlers) == 1, (
                f"{name} should have one FileHandler pointing at {timing_log}; "
                f"got {logger.handlers}"
            )

        # The services should have non-null timer attributes (a real
        # QueryTimer, not the NULL_QUERY_TIMER singleton — note that
        # QueryTimer subclasses NullQueryTimer so isinstance(real, NullQT)
        # is True; identity check against the singleton is the right test).
        assert services.graph_store._query_timer is not NULL_QUERY_TIMER
        assert services.content_store._query_timer is not NULL_QUERY_TIMER
        assert services.retrieval_service._query_timer is not NULL_QUERY_TIMER

        # The vault timing thread should be running.
        assert services.timing_thread is not None
    finally:
        # Best-effort teardown.
        if services.timing_thread is not None:
            services.timing_thread.stop(timeout=1.0)
        await services.graph_store.close()
        for name in ("sage.storage.timing", "sage.content.timing", "sage.retrieval.timing"):
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
            # Restore propagation so later caplog-based tests can still
            # capture records on these loggers (mcp_init disables
            # propagation when wiring per-vault file handlers).
            logger.propagate = True


# ── 10. VaultTimingThread smoke test ─────────────────────────────────


def test_vault_timing_thread_flushes_periodically(caplog):
    """Daemon thread fires flush() at the configured interval."""
    timer = QueryTimer(
        logger_name="sage.storage.timing",
        config=TimingConfig(
            emit_threshold_ms=10_000.0,
            summary_interval_seconds=0.05,
        ),
        layer="storage",
    )
    # Pre-load some suppressed counts.
    with timer.measure("get_document"):
        pass
    with timer.measure("get_document"):
        pass

    flush_thread = VaultTimingThread(
        timers=[timer],
        interval_seconds=0.05,
    )
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="sage.storage.timing"):
        flush_thread.start()
        # Poll for patience under CI load (the interval is 50 ms but CI
        # scheduling jitter can stretch the wait). The autouse
        # _stop_leaked_vault_timing_threads fixture in tests/sage/conftest.py
        # ensures no earlier test's VaultTimingThread is still flushing
        # into this caplog window, so summaries[0] is this timer's
        # summary — no need to filter by suppressed key.
        deadline = time.monotonic() + 1.0
        summaries: list[dict] = []
        while time.monotonic() < deadline:
            summaries = [
                p for p in _payloads(caplog, "sage.storage.timing") if p.get("summary") is True
            ]
            if summaries:
                break
            time.sleep(0.05)
        flush_thread.stop(timeout=1.0)

    assert len(summaries) >= 1, "expected at least one summary record from the flusher"
    assert summaries[0]["suppressed"]["get_document"] == 2
