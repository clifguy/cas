"""Bounded, correlated observations of ingestion work and worker waits.

Stage totals can overlap and must not be added to infer wall-clock latency.
One summary is emitted per operation after its detached workers actually finish.
No source text, exception messages or per-input history is retained.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Executor
from datetime import datetime, timezone
from typing import Any, TypeVar

_LOG = logging.getLogger("sage.ingestion.performance")
_TRACE: contextvars.ContextVar[Trace | None] = contextvars.ContextVar(
    "performance_trace", default=None
)
_MAX_KEYS = 32
_T = TypeVar("_T")


class Trace:
    """Thread-safe fixed-cardinality aggregates for one operation."""

    def __init__(self, vault_id: str, document_id: str, operation: str) -> None:
        self.vault_id = vault_id
        self.document_id = document_id
        self.operation = operation
        self.operation_id = uuid.uuid4().hex
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started_ns = time.perf_counter_ns()
        self.stages: dict[str, dict[str, int | float]] = {}
        self.counts: dict[str, int] = {}
        self.ranges: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()
        self._pending = 1
        self._outcome = "success"

    @staticmethod
    def _key(values: dict, name: str) -> str:
        return name if name in values or len(values) < _MAX_KEYS else "other"

    def count(self, name: str, value: int) -> None:
        with self._lock:
            key = self._key(self.counts, name)
            self.counts[key] = self.counts.get(key, 0) + value

    def extent(self, name: str, value: int) -> None:
        with self._lock:
            key = self._key(self.ranges, name)
            row = self.ranges.setdefault(key, {"minimum": value, "maximum": value})
            row["minimum"] = min(row["minimum"], value)
            row["maximum"] = max(row["maximum"], value)

    def observe(self, name: str, start: int, end: int, outcome: str = "success") -> None:
        with self._lock:
            key = self._key(self.stages, name)
            row = self.stages.setdefault(
                key,
                {
                    "count": 0,
                    "total_ms": 0.0,
                    "failed": 0,
                    "cancelled": 0,
                    "first_start_ns": start,
                    "last_end_ns": end,
                },
            )
            row["count"] += 1
            row["total_ms"] += max(0, end - start) / 1_000_000
            row["first_start_ns"] = min(row["first_start_ns"], start)
            row["last_end_ns"] = max(row["last_end_ns"], end)
            if outcome in ("failed", "cancelled"):
                row[outcome] += 1

    def mark_failed(self) -> None:
        with self._lock:
            self._outcome = "failed"

    def retain(self) -> None:
        with self._lock:
            self._pending += 1

    def finish(self, outcome: str | None = None) -> None:
        with self._lock:
            if outcome is not None and (outcome != "success" or self._outcome == "success"):
                self._outcome = outcome
            self._pending -= 1
            if self._pending:
                return
            payload = {
                "layer": "ingestion",
                "label": "performance",
                "operation_id": self.operation_id,
                "operation": self.operation,
                "vault_id": self.vault_id,
                "document_id": self.document_id,
                "started_at": self.started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": (time.perf_counter_ns() - self.started_ns) / 1_000_000,
                "outcome": self._outcome,
                "stages": self.stages,
                "counts": self.counts,
                "ranges": self.ranges,
                "stage_totals_may_overlap": True,
            }
        # Observability must not change the operation's result if a sink fails.
        try:
            _LOG.info(json.dumps(payload))
        except Exception:  # noqa: S110 -- a failed telemetry sink must not alter delivery
            pass


def current_trace() -> Trace | None:
    return _TRACE.get()


def clear_trace() -> None:
    """Detach long-lived queue workers from the request that created them."""
    _TRACE.set(None)


@contextlib.contextmanager
def scope(
    vault_id: str, document_id: str, operation: str, enabled: bool = True
) -> Iterator[Trace | None]:
    parent = current_trace()
    if enabled and parent is not None:
        yield parent
        return
    trace = Trace(vault_id, document_id, operation) if enabled else None
    token = _TRACE.set(trace)
    outcome = "success"
    try:
        yield trace
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except BaseException:
        outcome = "failed"
        raise
    finally:
        _TRACE.reset(token)
        if trace is not None:
            trace.finish(outcome)


@contextlib.contextmanager
def span(name: str) -> Iterator[None]:
    trace = current_trace()
    if trace is None:
        yield
        return
    start = time.perf_counter_ns()
    outcome = "success"
    try:
        yield
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except BaseException:
        outcome = "failed"
        raise
    finally:
        trace.observe(name, start, time.perf_counter_ns(), outcome)


async def offload(executor: Executor, label: str, work: Callable[[], _T]) -> _T:
    """Observe actual executor entry, retaining cancelled running work until exit."""
    trace = current_trace()
    context = contextvars.copy_context()
    queued = time.perf_counter_ns()
    if trace is not None:
        trace.retain()

    def run() -> _T:
        if trace is not None:
            trace.observe(label + ".queue", queued, time.perf_counter_ns())
        with span(label + ".execute"):
            return work()

    try:
        submitted = executor.submit(context.run, run)
    except BaseException:
        if trace is not None:
            trace.finish()
        raise
    # Completion releases retention for both cancelled queued and running work.
    # A running callback cannot be stopped; keep its summary until actual exit.
    if trace is not None:
        submitted.add_done_callback(lambda done: trace.finish())
    future = asyncio.wrap_future(submitted)
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        if submitted.cancel() and trace is not None:
            trace.observe(label + ".queue", queued, time.perf_counter_ns(), "cancelled")
        raise


def observe_operation(operation: str) -> Callable:
    """Establish service-bound context without changing service signatures."""

    def decorate(method: Callable) -> Callable:
        signature = inspect.signature(method)

        @functools.wraps(method)
        async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
            bound = signature.bind(self, *args, **kwargs).arguments
            job = bound.get("job")
            document_id = bound.get("document_id", getattr(job, "document_id", ""))
            if not document_id:
                document_id = getattr(bound.get("doc"), "id", "")
            chunks = bound.get("chunks")
            if not document_id and chunks:
                document_id = chunks[0].document_id
            enabled = self._config.timing.enabled
            with scope(self._config.vault.id, document_id, operation, enabled) as trace:
                if trace is not None and job is not None:
                    trace.observe("pipeline.queue", job.queued_ns, time.perf_counter_ns())
                with span(operation):
                    result = await method(self, *args, **kwargs)
                if trace is not None and hasattr(result, "document"):
                    trace.document_id = result.document.id
                return result

        return wrapped

    return decorate


@contextlib.asynccontextmanager
async def observed_context(manager: Any, enter: str, exit: str, failure: str) -> Any:
    """Time context entry and its real exit, including commit/rollback failures."""
    with span(enter):
        value = await manager.__aenter__()
    try:
        yield value
    except BaseException as exc:
        with span(failure):
            suppressed = await manager.__aexit__(type(exc), exc, exc.__traceback__)
        if not suppressed:
            raise
    else:
        with span(exit):
            await manager.__aexit__(None, None, None)
