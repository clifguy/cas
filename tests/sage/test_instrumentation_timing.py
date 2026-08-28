"""Structured query-timing instrumentation across SAGE layers.

Each test holds the line on one acceptance criterion from the plan:

1. test_storage_emits_record_for_get_document — happy path: storage layer.
2. test_content_emits_record_for_search_semantic — happy path: content layer.
3. test_retrieval_emits_request_record_with_phases — happy path: retrieval.
4. test_warn_threshold_drives_warning_level — slow-query WARN path.
5. test_fast_path_suppresses_and_summary_aggregates — fast-path + flush.
6. test_null_query_timer_default_is_silent — no-op default safety.
7. test_write_path_emits_storage_record — timer threading through writes.
8. test_vault_config_back_compat_no_timing_block — Pydantic default round-trip.
9. test_mcp_init_attaches_per_vault_file_handler — wiring confirmation.
10. test_abstraction_records_land_in_timing_log — records reach the file.
11. test_every_production_timing_logger_is_registered — bug-class gate.
12. test_release_restores_prior_propagate — install/release symmetry.
13. test_propagate_restored_only_after_last_path_releases — multi-vault.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.config import VaultConfig
from sage.instrumentation import (
    NULL_QUERY_TIMER,
    QueryTimer,
    TimingConfig,
    VaultTimingThread,
)
from sage.mcp_init import _TIMING_LOGGER_NAMES
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
from sage.storage.postgres.graph_store import PostgresGraphStore


@pytest.fixture(autouse=True)
def _isolate_timing_loggers():
    """Reset every registered timing logger before AND after each test.

    Other suite-wide tests (notably the MCP lifespan tests) call
    initialize_services, which attaches a per-vault FileHandler and sets
    propagate=False on these loggers. Without isolation, caplog can't
    capture records on subsequent tests because the propagation path is
    broken. We also tear down to be polite to whatever runs next.

    Derived from the canonical name tuple so a newly registered timing
    logger is isolated here without a second edit.
    """
    names = _TIMING_LOGGER_NAMES
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


async def test_storage_emits_record_for_get_document(pg_pool, caplog):
    """Real PostgresGraphStore + real QueryTimer → one DEBUG record with the right JSON.

    Coverage transfer note: timer threading through the Postgres adapter used
    to ride on the embedded pair's tests alone; dropping the ``query_timer=``
    plumbing in PostgresGraphStore now fails here.
    """
    timer = QueryTimer(
        logger_name="sage.storage.timing",
        config=TimingConfig(emit_threshold_ms=0.0),
        layer="storage",
        vault_id="test_vault",
    )
    store = PostgresGraphStore(pg_pool, query_timer=timer)
    await store.initialize()
    await store.insert_document(_doc())
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="sage.storage.timing"):
        await store.get_document("deadbeef_t73")

    payloads = _payloads(caplog, "sage.storage.timing")
    get_doc_payloads = [p for p in payloads if p.get("label") == "get_document"]
    assert len(get_doc_payloads) == 1, payloads
    payload = get_doc_payloads[0]
    assert payload["layer"] == "storage"
    assert payload["vault_id"] == "test_vault"
    assert isinstance(payload["duration_ms"], float)
    assert payload["duration_ms"] >= 0.0

    records_for_label = [
        r
        for r in caplog.records
        if r.name == "sage.storage.timing"
        and json.loads(r.getMessage()).get("label") == "get_document"
    ]
    assert records_for_label[0].levelno == logging.DEBUG


# ── 2. content happy path ────────────────────────────────────────────


async def test_content_emits_record_for_search_semantic(pg_pool, caplog):
    """Real PostgresContentStore + real QueryTimer → record with layer=content.

    Same coverage-transfer rationale as the storage test above, for the
    content adapter's ``query_timer=`` plumbing.
    """
    timer = QueryTimer(
        logger_name="sage.content.timing",
        config=TimingConfig(emit_threshold_ms=0.0),
        layer="content",
        vault_id="test_vault",
    )
    store = PostgresContentStore(pg_pool, query_timer=timer)

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
        vault_id="test_vault",
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
    assert payload["vault_id"] == "test_vault"
    assert payload["mode"] == RetrievalMode.CATALOG.value
    assert isinstance(payload["phases"], dict)
    assert len(payload["phases"]) >= 1, "expected at least one named phase"
    for phase_name, phase_ms in payload["phases"].items():
        assert isinstance(phase_name, str) and phase_name
        assert isinstance(phase_ms, float)
        assert phase_ms >= 0.0


# ── 4. WARN threshold drives WARNING level ───────────────────────────


async def test_warn_threshold_drives_warning_level(pg_pool, caplog):
    """warn_threshold_ms=0 forces WARN on every emission; no sleep required."""
    timer = QueryTimer(
        logger_name="sage.storage.timing",
        config=TimingConfig(
            emit_threshold_ms=0.0,
            warn_threshold_ms=0.0,
        ),
        layer="storage",
        vault_id="test_vault",
    )
    store = PostgresGraphStore(pg_pool, query_timer=timer)
    await store.initialize()
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="sage.storage.timing"):
        await store.get_document("nonexistent")

    warnings = [
        r
        for r in caplog.records
        if r.name == "sage.storage.timing" and r.levelno == logging.WARNING
    ]
    assert len(warnings) >= 1, [(r.levelno, r.getMessage()) for r in caplog.records]


# ── 5. fast-path suppression + summary aggregation ───────────────────


async def test_fast_path_suppresses_and_summary_aggregates(pg_pool, caplog):
    """Sub-threshold queries are counted, not emitted; flush() emits one summary."""
    timer = QueryTimer(
        logger_name="sage.storage.timing",
        config=TimingConfig(
            emit_threshold_ms=10_000.0,
            summary_interval_seconds=60.0,
        ),
        layer="storage",
        vault_id="test_vault",
    )
    store = PostgresGraphStore(pg_pool, query_timer=timer)
    await store.initialize()
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
    assert summary["vault_id"] == "test_vault"
    assert isinstance(summary["interval_s"], float)


# ── 5b. every payload shape names its vault ──────────────────────────


def test_query_timer_records_carry_the_constructing_vaults_id(caplog):
    """All three payload shapes carry the id of the vault whose timer emitted them.

    Every loaded vault's file handler is attached to the same process-global
    loggers, so a record in a shared log is attributable only by what it
    carries. Anti-coincidental-pass: two timers with distinct vault ids are
    both constructed before either emits. A hardcoded string fails the
    second timer's summary; an id read from anywhere shared -- module
    state, a last-constructed value -- fails the first timer's per-call
    and request records, since the second construction would have
    overwritten it. Only an id held by the emitting instance passes all
    three shapes.
    """
    emitting = QueryTimer(
        logger_name="sage.storage.timing",
        config=TimingConfig(emit_threshold_ms=0.0),
        layer="storage",
        vault_id="test_vault",
    )
    suppressing = QueryTimer(
        logger_name="sage.storage.timing",
        config=TimingConfig(emit_threshold_ms=10_000.0),
        layer="storage",
        vault_id="other_vault",
    )

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="sage.storage.timing"):
        with emitting.measure("get_document"):
            pass
        with emitting.request("catalog", "req-1"):
            pass
        with suppressing.measure("get_document"):
            pass
        suppressing.flush()

    payloads = _payloads(caplog, "sage.storage.timing")
    measures = [p for p in payloads if p.get("label") == "get_document"]
    requests = [p for p in payloads if p.get("label") == "request:catalog"]
    summaries = [p for p in payloads if p.get("summary") is True]
    assert len(measures) == 1 and len(requests) == 1 and len(summaries) == 1, payloads
    assert measures[0]["vault_id"] == "test_vault"
    assert requests[0]["vault_id"] == "test_vault"
    assert summaries[0]["vault_id"] == "other_vault"


# ── 6. no-op default is silent ───────────────────────────────────────


async def test_null_query_timer_default_is_silent(pg_pool, caplog):
    """Default kwarg = NULL_QUERY_TIMER: real query emits zero records."""
    store = PostgresGraphStore(pg_pool)
    await store.initialize()
    caplog.clear()
    with caplog.at_level(
        logging.DEBUG,
        logger="sage.storage.timing",
    ):
        await store.get_document("nonexistent")

    records = [r for r in caplog.records if r.name.endswith(".timing")]
    assert records == [], records


# ── 7. uninstrumented method emits nothing ───────────────────────────


async def test_write_path_emits_storage_record(pg_pool, caplog):
    """Write ops are instrumented on the Postgres adapter: insert_document
    emits exactly one storage-layer record.

    (The retired embedded store deliberately left writes unmeasured; the
    Postgres adapter measures every dispatch, so this pins timer threading
    through the write path rather than the old writes-out-of-scope rule.)
    """
    timer = QueryTimer(
        logger_name="sage.storage.timing",
        config=TimingConfig(emit_threshold_ms=0.0),
        layer="storage",
        vault_id="test_vault",
    )
    store = PostgresGraphStore(pg_pool, query_timer=timer)
    await store.initialize()
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="sage.storage.timing"):
        await store.insert_document(_doc())

    inserts = [
        p for p in _payloads(caplog, "sage.storage.timing") if p.get("label") == "insert_document"
    ]
    assert len(inserts) == 1, inserts
    assert inserts[0]["layer"] == "storage"


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
    """initialize_services attaches a per-vault FileHandler to every timing logger."""
    from tests.sage.conftest import initialize_services_for_test

    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")

    # Drop any leftover handlers from prior tests so the assertion is precise.
    for name in _TIMING_LOGGER_NAMES:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)

    config = VaultConfig.model_validate(minimal_vault_config_dict)
    # Build both stores via mcp_init's production path (the stack storage
    # provisioner) so the storage_timer/content_timer threading is verified
    # on the real wiring, not on injected stubs.
    async with initialize_services_for_test(config) as services:
        try:
            # Every registered logger got a handler pointing at
            # brain_root/timing.log
            brain_root = Path(config.vault.brain_root)
            timing_log = brain_root / "timing.log"
            for name in _TIMING_LOGGER_NAMES:
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

            # The production wiring must pass the vault's own id through
            # _build_vault_timers, not a constant: a constructor-level test
            # cannot see that pass-through.
            assert services.graph_store._query_timer._vault_id == config.vault.id
            assert services.content_store._query_timer._vault_id == config.vault.id
            assert services.retrieval_service._query_timer._vault_id == config.vault.id

            # The vault timing thread should be running.
            assert services.timing_thread is not None
        finally:
            # Handler cleanup runs before the helper exits. The helper
            # then stops the timing thread and closes the graph store.
            for name in _TIMING_LOGGER_NAMES:
                logger = logging.getLogger(name)
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                # Restore propagation so later caplog-based tests can still
                # capture records on these loggers (mcp_init disables
                # propagation when wiring per-vault file handlers).
                logger.propagate = True


# ── timing-handler reference counting ────────────────────────────────


def test_install_timing_handler_is_reference_counted(tmp_path):
    """A second install for the same log path reuses the handler and bumps a
    reference count; the open file handle is released only when the LAST
    reference is dropped. This is the property that keeps a same-brain_root
    vault reload logging across the old->new services swap, and that closes
    the timing.log handle on final teardown.

    Trap (anti-coincidental): if install opened a second file instead of
    refcounting, or release closed on the first drop, the mid-state
    assertion — still attached and open after exactly one release — fails.
    """
    from sage.mcp_init import _install_timing_handler, _release_timing_handler

    names = ("sage.storage.timing", "sage.content.timing", "sage.retrieval.timing")
    log_path = tmp_path / "timing.log"

    # First reference (vault load): opens the file and attaches the handler.
    h1 = _install_timing_handler(log_path)
    # Second reference (the reload's freshly-built services): reuses it.
    h2 = _install_timing_handler(log_path)
    assert h2 is h1, "second install for the same path must reuse the handler"
    for name in names:
        assert h1 in logging.getLogger(name).handlers
    assert h1.stream is not None and not h1.stream.closed

    # Drop the OLD services' reference. The surviving vault still holds one,
    # so the handler must stay attached and open.
    _release_timing_handler(h1)
    for name in names:
        assert h1 in logging.getLogger(name).handlers, (
            "handler was closed out from under the surviving vault on reload"
        )
    assert not h1.stream.closed

    # Drop the last reference: now it detaches from all three loggers and
    # closes the file handle.
    _release_timing_handler(h1)
    for name in names:
        assert h1 not in logging.getLogger(name).handlers
    assert h1.stream is None or h1.stream.closed

    # A redundant release is a harmless no-op (idempotent teardown).
    _release_timing_handler(h1)


def test_release_timing_handler_ignores_none_and_unknown():
    """``_release_timing_handler`` is a no-op for a None handler (timing
    disabled) and for a handler it never tracked, so every teardown path can
    call it unconditionally and more than once without raising.
    """
    from sage.mcp_init import _release_timing_handler

    _release_timing_handler(None)  # timing-disabled path: must not raise

    untracked = logging.Handler()  # never installed into the registry
    _release_timing_handler(untracked)  # must not raise, must not close anything


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
        vault_id="test_vault",
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
        # scheduling jitter can stretch the wait). The autouse timing-leak
        # guard in the root tests/conftest.py reaps any earlier test's leaked
        # VaultTimingThread, so none is still flushing into this caplog window
        # and summaries[0] is this timer's summary — no need to filter by
        # suppressed key.
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


# ── abstraction-layer records reach the per-vault log ─────────────────


def test_abstraction_records_land_in_timing_log(tmp_path):
    """Both abstraction emitters write into the per-vault ``timing.log`` file.

    Trap (anti-coincidental): the pre-existing tests for these records assert
    via ``caplog``, which captures through propagation and therefore passes
    whether or not the file handler is attached — the reason a logger absent
    from ``_TIMING_LOGGER_NAMES`` went unnoticed. This reads the file back off
    disk instead, so dropping the abstraction logger from the registered names
    fails it.

    The emitters are imported as objects rather than re-fetched by name, so a
    rename on either production side breaks this test rather than silently
    testing a logger nobody emits on.
    """
    from sage.adapters.abstraction_qwen3 import timing_logger as adapter_logger
    from sage.mcp_init import _install_timing_handler, _release_timing_handler
    from sage.services.ingestion import abstraction_timing_logger as service_logger

    log_path = tmp_path / "timing.log"
    handler = _install_timing_handler(log_path)
    try:
        service_logger.info(json.dumps({"layer": "abstraction", "label": "abstract"}))
        adapter_logger.info(json.dumps({"layer": "abstraction", "label": "generate_sync"}))
        handler.flush()
        written = log_path.read_text()
    finally:
        _release_timing_handler(handler)

    payloads = [json.loads(line.split(" ", 3)[3]) for line in written.splitlines() if line.strip()]
    labels = {p["label"] for p in payloads if p.get("layer") == "abstraction"}
    assert labels == {"abstract", "generate_sync"}, (
        f"both abstraction records should reach {log_path}; got {written!r}"
    )


def test_faithfulness_record_lands_in_timing_log(tmp_path):
    """The abstraction faithfulness record reaches the per-vault log file.

    Trap (anti-coincidental): the seam tests for this record assert via
    ``caplog``, which captures through propagation and passes whether or
    not the logger has a file handler. This reads the file back off disk,
    so a faithfulness logger missing from the registered names -- whose
    breach records would reach only the console -- fails it.
    """
    from sage.mcp_init import _install_timing_handler, _release_timing_handler
    from sage.services.ingestion import abstraction_faithfulness_logger

    log_path = tmp_path / "timing.log"
    handler = _install_timing_handler(log_path)
    try:
        abstraction_faithfulness_logger.info(
            json.dumps({"layer": "abstraction", "label": "unattested_gloss"})
        )
        handler.flush()
        written = log_path.read_text()
    finally:
        _release_timing_handler(handler)

    payloads = [json.loads(line.split(" ", 3)[3]) for line in written.splitlines() if line.strip()]
    labels = {p["label"] for p in payloads if p.get("layer") == "abstraction"}
    assert labels == {"unattested_gloss"}, (
        f"the faithfulness record should reach {log_path}; got {written!r}"
    )


def test_every_production_timing_logger_is_registered():
    """Every ``*.timing`` logger name in production code is a registered name.

    Gates the defect *class* rather than the instance: a new timing emitter
    whose logger is never added to ``_TIMING_LOGGER_NAMES`` gets no file
    handler, so its records reach only the process's console.

    Trap (anti-coincidental): a scan that globbed nothing would satisfy the
    membership assertion vacuously forever, so the collected set is asserted
    non-empty first.
    """
    import ast

    from sage.mcp_init import _TIMING_LOGGER_NAMES

    sage_root = Path(__file__).resolve().parents[2] / "sage"
    found: set[str] = set()
    for module_path in sage_root.rglob("*.py"):
        tree = ast.parse(module_path.read_text(), filename=str(module_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("sage.") and node.value.endswith(".timing"):
                    found.add(node.value)

    assert found, f"scan of {sage_root} collected no timing logger names — detector is broken"
    unregistered = found - set(_TIMING_LOGGER_NAMES)
    assert not unregistered, (
        f"timing loggers emitted on by production code but absent from "
        f"_TIMING_LOGGER_NAMES (their records never reach timing.log): {sorted(unregistered)}"
    )


def test_release_restores_prior_propagate(tmp_path):
    """Releasing the last handler restores each logger's pre-install ``propagate``.

    ``_install_timing_handler`` disables propagation so timing records don't
    bleed into the app stream. Without the inverse on release, a logger stays
    non-propagating for the life of the process — which silently breaks every
    later ``caplog`` assertion on that logger.

    Trap (anti-coincidental): one logger is seeded ``propagate=False`` before
    the install, so an implementation that unconditionally restores ``True``
    fails here rather than passing on the default.
    """
    from sage.mcp_init import (
        _TIMING_LOGGER_NAMES,
        _install_timing_handler,
        _release_timing_handler,
    )

    seeded = _TIMING_LOGGER_NAMES[0]
    logging.getLogger(seeded).propagate = False
    before = {name: logging.getLogger(name).propagate for name in _TIMING_LOGGER_NAMES}

    handler = _install_timing_handler(tmp_path / "timing.log")
    for name in _TIMING_LOGGER_NAMES:
        assert logging.getLogger(name).propagate is False, (
            f"{name} should not propagate while a timing handler is attached"
        )

    _release_timing_handler(handler)

    after = {name: logging.getLogger(name).propagate for name in _TIMING_LOGGER_NAMES}
    assert after == before, (
        f"release should restore the pre-install propagate state; {before} -> {after}"
    )


def test_propagate_restored_only_after_last_path_releases(tmp_path):
    """Propagation stays disabled while any other vault's handler is attached.

    Two vaults resolve to two ``timing.log`` paths and so to two registry
    entries, but they share the process-global timing loggers. Restoring
    ``propagate`` when the first entry releases would duplicate the surviving
    vault's records into the app stream.

    Trap (anti-coincidental): a per-entry snapshot/restore (rather than one
    keyed on the registry emptying) passes the single-path test above and
    fails here.
    """
    from sage.mcp_init import (
        _TIMING_LOGGER_NAMES,
        _install_timing_handler,
        _release_timing_handler,
    )

    paths = []
    for vault in ("vault_a", "vault_b"):
        brain_root = tmp_path / vault
        brain_root.mkdir()
        paths.append(brain_root / "timing.log")

    first = _install_timing_handler(paths[0])
    second = _install_timing_handler(paths[1])
    assert first is not second, "distinct log paths must get distinct handlers"

    _release_timing_handler(first)
    for name in _TIMING_LOGGER_NAMES:
        assert logging.getLogger(name).propagate is False, (
            f"{name} must stay non-propagating while vault_b's handler is attached"
        )

    _release_timing_handler(second)
    for name in _TIMING_LOGGER_NAMES:
        assert logging.getLogger(name).propagate is True, (
            f"{name} should propagate again once the last handler is released"
        )
