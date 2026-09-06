"""Structured query-timing instrumentation for SAGE storage and retrieval.

Emits per-call timing records on three named loggers
(``sage.storage.timing``, ``sage.content.timing``,
``sage.retrieval.timing``) so later work on these paths can cite
measured before/after numbers rather than estimates.

The hot path is bounded by the requirement that timing wrappers add no
more than ~10us per call. Sub-millisecond queries are suppressed at the
emit boundary and counted into a periodic summary so the steady-state
log volume stays grep-able rather than fire-hose.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from collections import Counter
from collections.abc import Iterator

from pydantic import BaseModel, Field


class TimingConfig(BaseModel):
    """Per-vault timing instrumentation configuration."""

    enabled: bool = Field(
        default=True,
        description=(
            "Master switch. When false, mcp_init wires the no-op timer "
            "to all services and the per-vault timing.log is never opened."
        ),
    )
    log_path: str | None = Field(
        default=None,
        description=(
            "Absolute path for the per-vault timing log. When null "
            "(default), mcp_init resolves to ``{brain_root}/timing.log``."
        ),
    )
    emit_threshold_ms: float = Field(
        default=1.0,
        ge=0.0,
        description=(
            "Fast-path skip threshold. Measurements below this are not "
            "formatted or logged individually; they bump a per-label "
            "counter that the periodic summary record consolidates."
        ),
    )
    warn_threshold_ms: float = Field(
        default=100.0,
        ge=0.0,
        description=(
            "Measurements above this are logged at WARNING; otherwise "
            "at DEBUG. Configurable per-vault so test cases can drive "
            "the WARN path on fast queries by lowering it."
        ),
    )
    summary_interval_seconds: float = Field(
        default=60.0,
        gt=0.0,
        description=(
            "Period at which the background flusher emits a "
            "summary record containing the per-label suppressed counts "
            "accumulated since the previous flush."
        ),
    )


class PhaseCollector:
    """Accumulates per-phase durations within a single retrieval request.

    Single-task by contract. Do not share across ``asyncio.gather`` branches;
    the underlying ``{phase: duration_ms}`` dict is not locked because
    retrieval does not currently fan phases out concurrently.
    """

    __slots__ = ("_phases",)

    def __init__(self) -> None:
        self._phases: dict[str, float] = {}

    @contextlib.contextmanager
    def phase(self, name: str) -> Iterator[None]:
        start_ns = time.perf_counter_ns()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
            self._phases[name] = self._phases.get(name, 0.0) + elapsed_ms

    def snapshot(self) -> dict[str, float]:
        return dict(self._phases)


class _NullPhaseCollector:
    """No-op PhaseCollector used by NullQueryTimer.request."""

    __slots__ = ()

    @contextlib.contextmanager
    def phase(self, name: str) -> Iterator[None]:
        yield


_NULL_PHASE_COLLECTOR = _NullPhaseCollector()


class NullQueryTimer:
    """Zero-overhead timer used as the default for service constructors.

    Tests and code paths that don't opt into instrumentation use this
    singleton so existing fixtures (graph_store, content_store,
    retrieval_service) keep working without wiring a real timer.
    """

    __slots__ = ()

    @contextlib.contextmanager
    def measure(self, label: str, params: dict | None = None) -> Iterator[None]:
        yield

    @contextlib.contextmanager
    def request(self, mode: str, request_id: str) -> Iterator[_NullPhaseCollector]:
        yield _NULL_PHASE_COLLECTOR

    def flush(self) -> None:
        return


NULL_QUERY_TIMER = NullQueryTimer()


class QueryTimer(NullQueryTimer):
    """Per-layer structured timing emitter.

    One instance per (vault, layer). Layers are ``"storage"``,
    ``"content"``, ``"retrieval"``; the named logger mirrors the layer
    (``sage.storage.timing`` etc.). All emitted records carry a
    JSON-serialized payload as the log message so a downstream
    grep/jq pipeline can join across layers via the ``layer`` field.
    Every payload names the constructing vault in ``vault_id``: the
    loggers are process-global and every loaded vault's file handler is
    attached to each of them, so a record in a shared log is
    attributable to its vault only by what the record itself carries.
    """

    def __init__(self, logger_name: str, config: TimingConfig, layer: str, vault_id: str) -> None:
        self._logger = logging.getLogger(logger_name)
        self._config = config
        self._layer = layer
        self._vault_id = vault_id
        self._lock = threading.Lock()
        self._suppressed: Counter[str] = Counter()
        self._last_summary_ns = time.monotonic_ns()

    @contextlib.contextmanager
    def measure(self, label: str, params: dict | None = None) -> Iterator[None]:
        start_ns = time.perf_counter_ns()
        try:
            yield
        finally:
            duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
            self._emit_or_suppress(label, duration_ms, params)

    @contextlib.contextmanager
    def request(self, mode: str, request_id: str) -> Iterator[PhaseCollector]:
        collector = PhaseCollector()
        start_ns = time.perf_counter_ns()
        try:
            yield collector
        finally:
            duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
            self._emit_request(mode, request_id, duration_ms, collector.snapshot())

    def flush(self) -> None:
        with self._lock:
            if not self._suppressed:
                return
            interval_s = (time.monotonic_ns() - self._last_summary_ns) / 1_000_000_000.0
            payload = {
                "layer": self._layer,
                "vault_id": self._vault_id,
                "summary": True,
                "interval_s": interval_s,
                "suppressed": dict(self._suppressed),
            }
            self._suppressed.clear()
            self._last_summary_ns = time.monotonic_ns()
        self._logger.debug(json.dumps(payload))

    def _emit_or_suppress(
        self,
        label: str,
        duration_ms: float,
        params: dict | None,
    ) -> None:
        if duration_ms < self._config.emit_threshold_ms:
            # Hot path: lock held only for the increment.
            with self._lock:
                self._suppressed[label] += 1
            return
        payload: dict[str, object] = {
            "layer": self._layer,
            "vault_id": self._vault_id,
            "label": label,
            "duration_ms": duration_ms,
        }
        if params:
            payload["params"] = params
        level = logging.WARNING if duration_ms > self._config.warn_threshold_ms else logging.DEBUG
        self._logger.log(level, json.dumps(payload))

    def _emit_request(
        self,
        mode: str,
        request_id: str,
        duration_ms: float,
        phases: dict[str, float],
    ) -> None:
        payload = {
            "layer": self._layer,
            "vault_id": self._vault_id,
            "label": f"request:{mode}",
            "mode": mode,
            "request_id": request_id,
            "duration_ms": duration_ms,
            "phases": phases,
        }
        level = logging.WARNING if duration_ms > self._config.warn_threshold_ms else logging.DEBUG
        self._logger.log(level, json.dumps(payload))


class VaultTimingThread:
    """Background daemon that flushes per-vault timers on a fixed interval.

    Owns no timer state itself; just walks the supplied timers and calls
    ``flush()`` on each. The thread exits cleanly when ``stop()`` is called
    from the vault-shutdown path.
    """

    def __init__(
        self,
        timers: list[QueryTimer],
        interval_seconds: float,
        thread_name: str = "sage-timing-flush",
    ) -> None:
        self._timers = list(timers)
        self._interval = interval_seconds
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=thread_name,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self._interval):
            for timer in self._timers:
                # The flusher must never crash the daemon, so the broad
                # except-and-pass below (suppressed via BLE001, S110) is
                # intentional. If the timing logger itself is broken,
                # logging the failure would also fail; silent skip is
                # intentional.
                try:
                    timer.flush()
                except Exception:  # noqa: BLE001, S110
                    pass
