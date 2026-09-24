"""Wait for a document to become safe to act on.

A test that waits on a document's background ingestion pipeline and then acts
on that document needs two things at once, not one:

1. ``pipeline_status`` is terminal -- no further abstraction begins from
   ``abstraction_complete``, ``abstraction_skipped`` or ``failed``.
2. The abstraction queue holds no in-flight claim on the document.

Both are required, and the second is the one that is easy to miss. The worker
releases the claim in its ``finally``, which runs *after* the terminal status
write -- on the completion path it refreshes the document's synthetic header
chunk in between, so the window spans a content-store write rather than a
scheduling hairline. A wait keyed on status alone can therefore return inside
that window, and the operator-facing entry points (re-abstraction, pipeline
recompute) reject a document whose claim is held. The caller proceeds from
exactly the condition its own next call refuses, and passes only when the poll
happens to observe the terminal status after the release. That is green on a
developer workstation and intermittently red under load.

This module is the one place that predicate is written. ``service`` is a
required keyword argument on both entry points, so the claim arm is not
something a caller can forget to opt into: a wait that compiles is a wait that
checks the claim. ``tests/test_pipeline_poll_discipline.py`` enforces that no
test module grows a second, status-only copy.

``accept`` widens the status arm for a caller that legitimately waits on a
state outside the default set -- ``abstraction_interrupted``, say, which is
settled but is not one of the three states the claim release is keyed to.
Widening it cannot reintroduce the race the module exists to prevent, because
the claim arm is unconditional.

``drain_abstraction_queue`` is the same predicate at vault scope, for teardown:
it waits until no dispatched abstraction job is unfinished and no claim is
held, so a fixture can release its storage without cutting work off mid-write.
It waits on the work itself rather than guessing how long the work takes, so
teardown costs what the pending work costs, and a stall fails loudly instead of
racing the close.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any, Final, Protocol

from sage.models.enums import PipelineStatus

# The states from which no further abstraction begins, so the in-flight claim
# is released or about to be. Deliberately narrower than
# ``sage.models.enums.TERMINAL_PIPELINE_STATUS_VALUES``, which also admits
# ``abstraction_interrupted``: that state is settled, but it is reached by the
# worker being stopped rather than by a job terminating, so it is not evidence
# about the claim either way. A caller that wants it passes ``accept``.
TERMINAL_PIPELINE_STATES: Final[frozenset[str]] = frozenset(
    {
        PipelineStatus.ABSTRACTION_COMPLETE.value,
        PipelineStatus.ABSTRACTION_SKIPPED.value,
        PipelineStatus.FAILED.value,
    }
)

DEFAULT_ATTEMPTS: Final[int] = 400
DEFAULT_DELAY: Final[float] = 0.01


class _ClaimHolder(Protocol):
    """Whatever owns the per-document in-flight claim registry."""

    _inflight: Mapping[str, Any]


def _status_of(payload: Any) -> str | None:
    """The ``pipeline_status`` of a document, in either shape it arrives in.

    The graph store returns a ``Document``; the tool surface returns parsed
    JSON. Both spellings reduce to the wire value so one accept-set serves
    both.
    """
    if payload is None:
        return None
    if isinstance(payload, Mapping):
        status = payload.get("pipeline_status")
    else:
        status = getattr(payload, "pipeline_status", None)
    return status.value if isinstance(status, PipelineStatus) else status


async def _await_idle(
    fetch: Callable[[], Awaitable[Any]],
    doc_id: str,
    *,
    service: _ClaimHolder,
    accept: frozenset[str],
    attempts: int,
    delay: float,
) -> Any:
    """Poll ``fetch`` until the document is settled and unclaimed, and return it.

    Raises AssertionError on timeout, naming the document, the last status
    observed and the claim state, so a genuine stall stays distinguishable
    from the assertion the caller was about to make.
    """
    inflight = service._inflight
    status: str | None = None
    for _ in range(attempts):
        payload = await fetch()
        status = _status_of(payload)
        if status in accept and doc_id not in inflight:
            return payload
        await asyncio.sleep(delay)

    claim = "claim still held" if doc_id in inflight else "no claim held"
    raise AssertionError(
        f"document {doc_id} did not become idle within {attempts} attempts; "
        f"last observed pipeline_status={status!r} ({claim})"
    )


async def await_pipeline_idle(
    graph_store: Any,
    doc_id: str,
    *,
    service: _ClaimHolder,
    accept: frozenset[str] = TERMINAL_PIPELINE_STATES,
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY,
) -> Any:
    """Wait on the graph store until ``doc_id`` is settled and unclaimed.

    Returns the ``Document`` as it stood on the observation that satisfied
    both arms.
    """
    return await _await_idle(
        lambda: graph_store.get_document(doc_id),
        doc_id,
        service=service,
        accept=accept,
        attempts=attempts,
        delay=delay,
    )


async def await_tool_idle(
    fetch: Callable[[], Awaitable[Any]],
    doc_id: str,
    *,
    service: _ClaimHolder,
    accept: frozenset[str] = TERMINAL_PIPELINE_STATES,
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY,
) -> Any:
    """Wait on an arbitrary read path until ``doc_id`` is settled and unclaimed.

    ``fetch`` is an async zero-argument callable returning the document in
    whatever shape that path produces -- parsed JSON from a tool or an HTTP
    route, typically. Returns that payload.
    """
    return await _await_idle(
        fetch,
        doc_id,
        service=service,
        accept=accept,
        attempts=attempts,
        delay=delay,
    )


# Generous next to what a stub provider needs, and still a bound: a drain that
# reaches it is reporting work that will not finish, not work that is slow.
DEFAULT_DRAIN_TIMEOUT: Final[float] = 30.0


def _vault_label(service: Any) -> str:
    """The vault id a service belongs to, for failure messages."""
    config = getattr(service, "_config", None)
    vault = getattr(config, "vault", None)
    return str(getattr(vault, "id", "<unknown vault>"))


def _pending_description(service: Any) -> str:
    queue = service._abstraction_queue
    unfinished = 0 if queue is None else queue._unfinished_tasks
    claims = sorted(service._inflight)
    return f"{unfinished} unfinished abstraction job(s); claims held on {claims}"


async def drain_abstraction_queue(
    service: Any,
    *,
    timeout: float | None = None,
    delay: float = DEFAULT_DELAY,
) -> None:
    """Wait until the service's abstraction work is finished and its claims released.

    Two arms, both required. ``queue.join()`` returns once every dispatched
    job has run to completion -- the worker marks a job done only after the
    job has released its claim. A claim can also be held with no job queued
    yet: re-abstraction and pipeline recompute claim a document before they
    enqueue, and await in between. So after the join the claim registry is
    polled until empty.

    A service whose queue was never created has dispatched nothing and is
    drained as it stands; the queue is not created here. A queue holding
    unfinished jobs with no live worker can never drain, and is reported at
    once rather than after the timeout.

    ``timeout`` defaults to ``DEFAULT_DRAIN_TIMEOUT``, read at call time.
    Raises AssertionError naming the vault and the pending work when the
    bound is reached.
    """
    if timeout is None:
        timeout = DEFAULT_DRAIN_TIMEOUT
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    queue = service._abstraction_queue
    if queue is not None and queue._unfinished_tasks:
        worker = service._worker_task
        if worker is None or worker.done():
            raise AssertionError(
                f"vault {_vault_label(service)}: abstraction worker not running with "
                f"{_pending_description(service)}; the queue cannot drain"
            )
        try:
            await asyncio.wait_for(queue.join(), timeout)
        except TimeoutError:
            raise AssertionError(
                f"vault {_vault_label(service)} did not drain within {timeout}s: "
                f"{_pending_description(service)}"
            ) from None
    while service._inflight:
        if loop.time() >= deadline:
            raise AssertionError(
                f"vault {_vault_label(service)} did not drain within {timeout}s: "
                f"{_pending_description(service)}"
            )
        await asyncio.sleep(delay)


async def drain_vaults(
    registry: Mapping[str, Any],
    vault_ids: Iterable[str] | None = None,
    *,
    timeout: float | None = None,
) -> None:
    """Drain the abstraction work of each named vault in a services registry.

    ``registry`` maps a vault id to its services bundle, as the app's vault
    registry does. ``vault_ids`` defaults to every entry; an id no longer in
    the registry is skipped, since there is nothing left to drain under it.
    """
    ids = list(registry) if vault_ids is None else list(vault_ids)
    for vault_id in ids:
        services = registry.get(vault_id)
        if services is not None:
            await drain_abstraction_queue(services.ingestion_service, timeout=timeout)
