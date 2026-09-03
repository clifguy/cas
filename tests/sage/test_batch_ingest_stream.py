"""Termination contract for the shared batch-ingest SSE generator.

``batch_ingest_sse_stream`` drives the batch pipeline in a background task
and drains a queue the task feeds; the consumer loop ends on a ``None``
sentinel the task enqueues last. These tests pin what happens when the task
raises before it gets there.

The decision this file guards is that a batch-level failure ends the stream
by propagating: the generator raises, and the caller's ``StreamingResponse``
is torn down. There is no in-stream SSE error event -- the same shape the
reabstract-deferred stream already has, and the reason every batch-ingest
refusal that *can* be decided before the response is committed is decided
there instead.

Every test bounds its wait. A regression here is a generator that never
terminates, which without a deadline hangs the run rather than failing it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import pytest

from sage.services.batch_ingest import FileDescriptor, IngestSummary
from sage.services.batch_ingest_stream import batch_ingest_sse_stream

# Every wait in this module is bounded by this. Generous against a slow
# machine, and short enough that a hang fails the test rather than the run.
STREAM_TIMEOUT = 5.0


def _fd(name: str = "a.md") -> FileDescriptor:
    return FileDescriptor(file_path=f"/tmp/{name}", source_type="markdown")


def _payloads(chunks: list[str]) -> list[dict]:
    """Parse the ``data:`` lines a run yielded into their JSON payloads."""
    return [json.loads(chunk.removeprefix("data: ").strip()) for chunk in chunks]


async def _drain(
    gen: AsyncGenerator[str, None],
    into: list[str],
    timeout: float = STREAM_TIMEOUT,
) -> None:
    """Consume ``gen`` into ``into``, requiring it to end on its own.

    Appends as it goes rather than returning a list, so a run that ends by
    raising still exposes what reached the client first.

    Deliberately waits *without* cancelling on the deadline, which is what
    makes the failure tests in this module discriminating. Cancelling would
    throw into the generator at its queue wait; the generator's
    ``finally: await task`` then re-raises the pipeline's own exception,
    which replaces the cancellation. A hung stream would therefore surface
    exactly the exception a terminating one surfaces, and every assertion
    below would pass against the hang. Waiting plainly keeps the two
    outcomes distinguishable, and the deadline reports the hang as itself.
    """

    async def _collect() -> None:
        async for chunk in gen:
            into.append(chunk)

    task = asyncio.create_task(_collect())
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task not in done:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise AssertionError(
            f"the stream did not terminate on its own within {timeout}s: the "
            "consumer is still waiting for a sentinel the pipeline never "
            "enqueued"
        )
    task.result()  # re-raise whatever ended the stream


def _patch_run(monkeypatch: pytest.MonkeyPatch, impl) -> None:
    """Replace ``BatchIngestService.run`` as the generator constructs it.

    The generator instantiates the service itself, so the class it resolves
    is the patch point.
    """
    service = MagicMock()
    service.run = impl
    monkeypatch.setattr(
        "sage.services.batch_ingest_stream.BatchIngestService",
        lambda: service,
    )


class TestPipelineFailureEndsTheStream:
    """A background-task exception terminates the stream deterministically."""

    async def test_stream_terminates_when_the_pipeline_task_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pipeline that raises ends the generator by re-raising, rather
        than leaving the consumer blocked on a sentinel that never arrives.

        Anti-coincidental-pass: the discriminating evidence is that the
        generator ends *without being cancelled*, which ``_drain`` reports
        separately from the exception. Against a generator whose sentinel is
        enqueued only on the success path, the consumer blocks forever and
        the deadline raises an ``AssertionError`` -- not the ``RuntimeError``
        this expects. Asserting the exception alone is not enough: a
        cancelled hang re-raises the pipeline's own ``RuntimeError`` out of
        the generator's ``finally: await task``, so a deadline that cancels
        would let the hang pass this test unchanged.
        """

        async def failing_run(**kwargs) -> IngestSummary:
            raise RuntimeError("pipeline boom")

        _patch_run(monkeypatch, failing_run)

        chunks: list[str] = []
        with pytest.raises(RuntimeError, match="pipeline boom"):
            await _drain(batch_ingest_sse_stream([_fd()], MagicMock()), chunks)

    async def test_progress_events_before_the_failure_still_reach_the_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Events the pipeline enqueued before it raised are delivered ahead
        of the termination: the run reports how far it got.

        Anti-coincidental-pass: the equality is ordered and names each
        event's ``filename`` and ``status``, so an implementation that
        drained the queue and discarded it, or that emitted a synthesized
        placeholder, fails. The batch dies partway through a second file, so
        three events are outstanding when it raises: a generator that
        delivered only the first, or that dropped the partial file's
        ``started`` because no ``completed`` followed it, fails the equality
        too.
        """

        async def failing_run(**kwargs) -> IngestSummary:
            await kwargs["on_file_start"](0, 2, "first.md")
            await kwargs["on_file_done"](0, 2, "first.md", "a1b2c3d4_first")
            await kwargs["on_file_start"](1, 2, "second.md")
            raise RuntimeError("pipeline boom")

        _patch_run(monkeypatch, failing_run)

        chunks: list[str] = []
        with pytest.raises(RuntimeError, match="pipeline boom"):
            await _drain(batch_ingest_sse_stream([_fd()], MagicMock()), chunks)

        payloads = _payloads(chunks)
        assert [(p["event_type"], p["filename"], p["status"]) for p in payloads] == [
            ("progress", "first.md", "started"),
            ("progress", "first.md", "completed"),
            ("progress", "second.md", "started"),
        ]

    async def test_the_pipeline_exception_is_logged_with_its_traceback(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The failure is logged where the operator will find it, with the
        traceback attached -- the stream itself carries no error payload, so
        the log is the only place the cause survives.

        Anti-coincidental-pass: the assertion is on ``exc_info`` being
        populated, which separates ``logger.exception`` from a bare
        ``logger.error`` that would satisfy any message-only check while
        discarding the traceback that names the raise site.
        """

        async def failing_run(**kwargs) -> IngestSummary:
            raise RuntimeError("pipeline boom")

        _patch_run(monkeypatch, failing_run)
        caplog.set_level(logging.ERROR, logger="sage.services.batch_ingest_stream")

        chunks: list[str] = []
        with pytest.raises(RuntimeError, match="pipeline boom"):
            await _drain(batch_ingest_sse_stream([_fd()], MagicMock()), chunks)

        records = [r for r in caplog.records if r.name == "sage.services.batch_ingest_stream"]
        assert len(records) == 1
        assert records[0].levelno == logging.ERROR
        assert records[0].exc_info is not None
        assert records[0].exc_info[0] is RuntimeError

    async def test_a_successful_run_still_ends_with_a_summary_and_no_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control for the three tests above: on the success path the stream
        ends with a ``summary`` event and the generator returns normally.

        Without this, a generator rewritten to raise unconditionally would
        satisfy every failure assertion in this class.
        """

        async def succeeding_run(**kwargs) -> IngestSummary:
            await kwargs["on_file_start"](0, 1, "a.md")
            return IngestSummary(docs_new=1)

        _patch_run(monkeypatch, succeeding_run)

        chunks: list[str] = []
        await _drain(batch_ingest_sse_stream([_fd()], MagicMock()), chunks)

        payloads = _payloads(chunks)
        assert [p["event_type"] for p in payloads] == ["progress", "summary"]
        assert payloads[-1]["documents_created"] == {"new": 1, "new_version": 0}
