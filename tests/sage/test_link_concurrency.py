"""Concurrency tests for GraphOpsService._create_edge_strict.

Covers the post-ADR-017 regression where parallel link calls could
interleave their pre-insert reads and inserts, and cancelled MCP calls
left orphaned in-flight work behind.

The fix (a) batches all pre-insert reads into a single store call via
GraphStore.read_link_context and (b) serializes GraphOpsService
._create_edge_strict with a service-level asyncio.Lock so parallel
callers queue cheaply instead of overlapping inside the store.
"""

import asyncio
import hashlib
import re
import time
from datetime import datetime, timezone

from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import Document, LinkRequest


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "a1" or "doc_a"; this helper wraps them so the values still
    construct valid LinkRequest / TraverseRequest / ChainRequest
    instances. Deterministic — the same name always yields the same id.
    """
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha(name: str) -> str:
    """Deterministic canonical Sha256 from a short test name.

    The Sha256Str validator requires `^sha256:[0-9a-f]{64}$`. Test
    fixtures historically used short readable strings like
    f"hash_{doc_id}" or "sha256:abc"; this helper maps any such
    name to a stable canonical Sha256. Idempotent.
    """
    if _SHA256_RE.fullmatch(name):
        return name
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{name}".encode()).hexdigest()


def _make_doc(doc_id: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Doc {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        lifecycle_status="active",
        source_content_hash=_sha(doc_id),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )


# ---------------------------------------------------------------------------
# Serialization: the asyncio.Lock gates concurrent link calls
# ---------------------------------------------------------------------------


async def test_link_serializes_concurrent_calls(graph_store, graph_ops_service, monkeypatch):
    """Concurrent link calls must not overlap inside the store.

    One link call's batched pre-insert read (read_link_context) and its
    insert_edge must never interleave with another call's — the
    interleaving window is where a stale read produces a duplicate or
    lost edge. We wrap both store entry points to count simultaneous
    in-flight calls; with the service-level lock in place, the peak
    should be 1. Dropping the lock lets the asyncio.sleep(0) yield below
    interleave the wrapped calls and the peak exceeds 1.
    """
    for name in ("doc_a", "doc_b", "doc_c", "doc_d"):
        await graph_store.insert_document(_make_doc(_id(name)))

    active = {"count": 0, "peak": 0}

    def _tracking(method):
        async def wrapper(*args, **kwargs):
            active["count"] += 1
            active["peak"] = max(active["peak"], active["count"])
            try:
                # Small yield to give other coroutines a chance to
                # interleave if the lock were not in place.
                await asyncio.sleep(0)
                return await method(*args, **kwargs)
            finally:
                active["count"] -= 1

        return wrapper

    monkeypatch.setattr(graph_store, "read_link_context", _tracking(graph_store.read_link_context))
    monkeypatch.setattr(graph_store, "insert_edge", _tracking(graph_store.insert_edge))

    # Four concurrent link calls on disjoint document pairs.
    async def do_link(src, tgt):
        return await graph_ops_service._create_edge_strict(
            LinkRequest(
                source_id=src,
                target_id=tgt,
                edge_type=EdgeType.REFERENCES,
                source_valid_from_version=src,
                target_valid_from_version=tgt,
            )
        )

    results = await asyncio.gather(
        do_link(_id("doc_a"), _id("doc_b")),
        do_link(_id("doc_c"), _id("doc_d")),
        do_link(_id("doc_a"), _id("doc_c")),
        do_link(_id("doc_b"), _id("doc_d")),
    )

    assert len(results) == 4
    # Link() now returns LinkResponse; unwrap to verify edges.
    assert all(r.edge.id for r in results)
    assert active["peak"] == 1, (
        f"expected only one link call in-flight at a time under the lock, "
        f"peak overlap was {active['peak']}"
    )


# ---------------------------------------------------------------------------
# Cancellation bound: cancelled link calls do not leak in-flight work
# ---------------------------------------------------------------------------


async def test_cancelled_parallel_link_calls_drain_quickly(graph_store, graph_ops_service):
    """Spawning many link tasks and cancelling them must drain fast.

    Before the fix, each in-flight link fanned out many independent
    store calls that kept running past asyncio cancellation; a dozen
    parallel calls could leave the store saturated with orphaned work,
    making the next operation block for minutes. With the batched-read
    fix and handler-level lock, at most one call is inside the store at
    a time; the rest queue at the asyncio layer and exit immediately on
    cancel.

    We assert a strict wall-clock bound: even N parallel cancels should
    settle in well under a second on any reasonable machine.
    """
    for i in range(20):
        await graph_store.insert_document(_make_doc(_id(f"d{i}")))

    # Spawn many concurrent link tasks, then cancel all after letting
    # the scheduler tick once so they can enter the lock queue.
    tasks = [
        asyncio.create_task(
            graph_ops_service._create_edge_strict(
                LinkRequest(
                    source_id=_id(f"d{i}"),
                    target_id=_id(f"d{(i + 1) % 20}"),
                    edge_type=EdgeType.REFERENCES,
                    source_valid_from_version=_id(f"d{i}"),
                    target_valid_from_version=_id(f"d{(i + 1) % 20}"),
                )
            )
        )
        for i in range(20)
    ]

    await asyncio.sleep(0)  # let tasks schedule
    for t in tasks:
        t.cancel()

    # Allow cancellation to propagate. Gather with return_exceptions so
    # CancelledError doesn't abort this coroutine.
    await asyncio.gather(*tasks, return_exceptions=True)

    # After cancellation, any remaining in-flight work should drain fast.
    # We measure the store's ability to respond: one more fast read.
    t0 = time.monotonic()
    await graph_store.get_total_edge_count()
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0, (
        f"store appears saturated post-cancellation: simple read took "
        f"{elapsed:.2f}s (expected < 2s)"
    )
