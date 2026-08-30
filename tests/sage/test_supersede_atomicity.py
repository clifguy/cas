"""Supersede transactional atomicity tests: BH-135, BH-136.

Pin the database-transaction guarantees of the supersede transition at
both the lifecycle service boundary and the ingestion service boundary.
The failure mode these tests prevent is the orphan class observed with
PV02: a successor record exists, the predecessor is still active, and
no supersedes edge was ever created.
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document, IngestRequest, SetLifecycleRequest


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


def _make_doc(
    doc_id: str,
    lifecycle_status: str = "active",
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Test {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        lifecycle_status=lifecycle_status,
        source_content_hash=_sha(doc_id),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=pipeline_status,
    )


def _seed_file(tmp_vault_dir: Path, relative: str, content: str) -> Path:
    full = tmp_vault_dir / "sources" / relative
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return full


async def _supersedes_edges(graph_store, source_id: str, target_id: str):
    edges = await graph_store.get_edges_by_source(source_id, edge_type="supersedes")
    return [e for e in edges if e.target_id == target_id]


# ---------------------------------------------------------------------------
# BH-135: Lifecycle supersede transition is atomic on edge-insert failure
# ---------------------------------------------------------------------------


async def test_bh_135_lifecycle_supersede_rolls_back_on_edge_failure(
    graph_store, lifecycle_service, monkeypatch
):
    """If the edge insert raises mid-transition, the predecessor's
    lifecycle_status must NOT be flipped. State is left consistent so
    the caller can retry without leaving partial state behind.
    """
    pred = _make_doc(_id("pred_135"), lifecycle_status="active")
    succ = _make_doc(_id("succ_135"), lifecycle_status="active")
    await graph_store.insert_document(pred)
    await graph_store.insert_document(succ)

    # supersede_atomic uses the inner _exec_insert_edge helper inside its
    # database transaction; patch that to provoke a mid-transaction failure.
    original_exec_insert_edge = graph_store._exec_insert_edge

    def failing_exec_insert_edge(conn, edge):
        raise RuntimeError("simulated lock contention on edges table")

    monkeypatch.setattr(graph_store, "_exec_insert_edge", failing_exec_insert_edge)

    with pytest.raises(RuntimeError, match="simulated lock contention"):
        await lifecycle_service._set_lifecycle(
            pred.id,
            SetLifecycleRequest(action="supersede", successor_id=succ.id),
        )

    # Predecessor must still be active: lifecycle flip rolled back.
    pred_after = await graph_store.get_document(pred.id)
    assert pred_after.lifecycle_status == "active", (
        "predecessor was left archived without a supersedes edge -- "
        "the transactional rollback did not fire"
    )
    # No supersedes edge persisted.
    assert await _supersedes_edges(graph_store, succ.id, pred.id) == []

    # Restore and retry: the same call should now succeed cleanly.
    monkeypatch.setattr(graph_store, "_exec_insert_edge", original_exec_insert_edge)
    response = await lifecycle_service._set_lifecycle(
        pred.id,
        SetLifecycleRequest(action="supersede", successor_id=succ.id),
    )
    assert response.document.lifecycle_status == "archived"
    edges = await _supersedes_edges(graph_store, succ.id, pred.id)
    assert len(edges) == 1


# ---------------------------------------------------------------------------
# BH-136: Ingest rolls back the new document when supersede fails
# ---------------------------------------------------------------------------


async def test_bh_136_ingest_rolls_back_doc_on_supersede_failure(
    tmp_vault_dir, graph_store, ingestion_service, monkeypatch
):
    """If the atomic insert+supersede transaction fails, the new doc
    record must NOT be persisted. Caller observes either a complete
    version chain or no new document at all.
    """
    _seed_file(tmp_vault_dir, "bh136_v1.md", "# V1\n\nOriginal.")
    _seed_file(tmp_vault_dir, "bh136_v2.md", "# V2\n\nRevised.")

    v1 = await ingestion_service.ingest(
        IngestRequest(source="bh136_v1.md", source_type=SourceType.MARKDOWN)
    )

    # Force the atomic insert+supersede primitive to raise mid-transaction,
    # simulating a DB lock / constraint failure during the commit. The
    # database transaction must roll back so the new doc record is not
    # persisted.
    original_atomic = graph_store.insert_with_supersede_atomic

    async def failing_atomic(*args, **kwargs):
        raise RuntimeError("simulated mid-transaction failure")

    monkeypatch.setattr(graph_store, "insert_with_supersede_atomic", failing_atomic)

    with pytest.raises(RuntimeError, match="simulated mid-transaction failure"):
        await ingestion_service.ingest(
            IngestRequest(
                source="bh136_v2.md",
                source_type=SourceType.MARKDOWN,
                predecessor_id=v1.document.id,
            )
        )

    # Only the v1 doc should exist; the v2 record should not have landed.
    docs = await graph_store.list_all_documents()
    assert [d.id for d in docs] == [v1.document.id], (
        "ingest left an orphan successor record after atomic supersede failure"
    )
    # Predecessor unchanged.
    pred_after = await graph_store.get_document(v1.document.id)
    assert pred_after.lifecycle_status == "active"
    # No supersedes edges anywhere.
    edges = await graph_store.get_edges_by_source(v1.document.id)
    assert [e for e in edges if e.edge_type == "supersedes"] == []

    # Restore and retry: the same ingest should now succeed cleanly.
    monkeypatch.setattr(graph_store, "insert_with_supersede_atomic", original_atomic)
    v2 = await ingestion_service.ingest(
        IngestRequest(
            source="bh136_v2.md",
            source_type=SourceType.MARKDOWN,
            predecessor_id=v1.document.id,
        )
    )
    pred_after = await graph_store.get_document(v1.document.id)
    assert pred_after.lifecycle_status == "archived"
    edges_after = await _supersedes_edges(graph_store, v2.document.id, v1.document.id)
    assert len(edges_after) == 1


# ---------------------------------------------------------------------------
# Batch edge inference: the settlement commit shares the same guarantees
# ---------------------------------------------------------------------------


async def test_batch_supersede_settlement_rolls_back_on_edge_failure(
    graph_store, lifecycle_service, lock_manager, graph_ops_service, monkeypatch
):
    """A batch settlement that fails mid-commit leaves neither half behind.

    The batch path commits the predecessor transition and the supersedes
    edge through the same atomic primitive as the lifecycle surface, so
    the BH-135 guarantee holds here too: no supersedes edge outlives an
    untransitioned target. The failing group's chain-repair removal is
    withheld as well -- the graph ends holding no fewer supersedes edges
    than it started with. Restoring the store and re-running the same
    plan converges cleanly, which is the recovery story the atomicity
    buys.
    """
    from sage.models.enums import EdgeType, RationaleKind
    from sage.models.schemas import Edge
    from sage.services.batch_inference import (
        EdgePlan,
        PlannedEdge,
        PlannedEdgeRemoval,
        resolve_and_execute,
    )

    pred = _make_doc(_id("pred_batch"), lifecycle_status="active")
    succ = _make_doc(_id("succ_batch"), lifecycle_status="active")
    old = _make_doc(_id("old_batch"), lifecycle_status="archived")
    for doc in (pred, succ, old):
        await graph_store.insert_document(doc)
    # The edge chain repair wants to remove, in the same repair group as
    # the add that will fail.
    old_edge = Edge(
        id="00000000-0000-4000-8000-00000000e001",
        source_id=succ.id,
        target_id=old.id,
        edge_type=EdgeType.SUPERSEDES,
        created_at=datetime.now(timezone.utc),
        rationale_kind=RationaleKind.VERSION_CHAIN,
    )
    await graph_store.insert_edge(old_edge)

    plan = EdgePlan(
        edges=[
            PlannedEdge(
                succ.id,
                pred.id,
                EdgeType.SUPERSEDES,
                1,
                "version_chain",
                "[version_chain] v2 supersedes v1",
                repair_group="grp",
            )
        ],
        removals=[
            PlannedEdgeRemoval(
                edge_id=old_edge.id,
                source_id=succ.id,
                target_id=old.id,
                edge_type=EdgeType.SUPERSEDES,
                reason="chain_repair: no longer in desired chain",
                repair_group="grp",
            )
        ],
    )

    original_exec_insert_edge = graph_store._exec_insert_edge
    fired = 0

    def failing_exec_insert_edge(conn, edge):
        nonlocal fired
        fired += 1
        raise RuntimeError("simulated edge-insert failure")

    monkeypatch.setattr(graph_store, "_exec_insert_edge", failing_exec_insert_edge)

    result = await resolve_and_execute(
        plan, {}, graph_store, graph_ops_service, lifecycle_service, lock_manager
    )

    assert fired > 0, "control: the injected failure never fired"
    # Neither half landed: target untransitioned, no edge pointing at it.
    pred_after = await graph_store.get_document(pred.id)
    assert pred_after.lifecycle_status == "active", (
        "the lifecycle flip survived a failed edge insert -- "
        "the transactional rollback did not fire"
    )
    assert await _supersedes_edges(graph_store, succ.id, pred.id) == []
    # The removal was withheld: the chain is no shorter than it was found.
    assert await _supersedes_edges(graph_store, succ.id, old.id) != []
    assert result.edges_removed == 0
    reasons = {w["reason"] for w in result.warnings}
    assert reasons == {"edge_creation_failed", "chain_repair_withheld"}

    # Restore and re-run: the same plan converges.
    monkeypatch.setattr(graph_store, "_exec_insert_edge", original_exec_insert_edge)
    result2 = await resolve_and_execute(
        plan, {}, graph_store, graph_ops_service, lifecycle_service, lock_manager
    )
    assert result2.edges_created == {"supersedes": 1}
    assert result2.edges_removed == 1
    pred_after = await graph_store.get_document(pred.id)
    assert pred_after.lifecycle_status == "archived"
    assert await graph_store.has_supersedes_successor(pred.id)
    committed = await _supersedes_edges(graph_store, succ.id, pred.id)
    assert len(committed) == 1
    assert committed[0].rationale_kind == RationaleKind.VERSION_CHAIN
    assert await _supersedes_edges(graph_store, succ.id, old.id) == []


async def test_batch_supersede_race_lands_exactly_one_settlement(
    graph_store, lifecycle_service, lock_manager, graph_ops_service, monkeypatch
):
    """Two concurrent settlements of one predecessor: exactly one lands.

    Both callers pass the advisory settlement check against the same
    still-active predecessor -- a barrier on the first two reads
    guarantees the overlap -- and the per-predecessor lock plus in-lock
    re-validation refuses the loser: one lifecycle write, one supersedes
    edge, and a `supersede_target_not_transitionable` warning for the
    other caller, instead of a forked chain.
    """
    import asyncio

    from sage.models.enums import EdgeType
    from sage.services.batch_inference import EdgePlan, PlannedEdge, resolve_and_execute

    pred = _make_doc(_id("pred_race"), lifecycle_status="active")
    succ_a = _make_doc(_id("succ_race_a"), lifecycle_status="active")
    succ_b = _make_doc(_id("succ_race_b"), lifecycle_status="active")
    for doc in (pred, succ_a, succ_b):
        await graph_store.insert_document(doc)

    # Barrier: the first two reads of the predecessor are the two
    # settlement reads; hold both until both have read, so each caller
    # passes the advisory check against the still-active state.
    settlement_reads = 0
    both_settled = asyncio.Event()
    real_get_document = graph_store.get_document

    async def synced_get_document(doc_id):
        nonlocal settlement_reads
        doc = await real_get_document(doc_id)
        if doc_id == pred.id:
            settlement_reads += 1
            if settlement_reads == 2:
                both_settled.set()
            if settlement_reads <= 2:
                await both_settled.wait()
        return doc

    monkeypatch.setattr(graph_store, "get_document", synced_get_document)

    def _plan(successor_id: str) -> EdgePlan:
        return EdgePlan(
            edges=[
                PlannedEdge(
                    successor_id,
                    pred.id,
                    EdgeType.SUPERSEDES,
                    1,
                    "version_chain",
                    "[version_chain] race",
                )
            ]
        )

    result_a, result_b = await asyncio.gather(
        resolve_and_execute(
            _plan(succ_a.id), {}, graph_store, graph_ops_service, lifecycle_service, lock_manager
        ),
        resolve_and_execute(
            _plan(succ_b.id), {}, graph_store, graph_ops_service, lifecycle_service, lock_manager
        ),
    )

    created = [r.edges_created.get("supersedes", 0) for r in (result_a, result_b)]
    assert sorted(created) == [0, 1], f"expected exactly one settlement, got {created}"
    all_warnings = result_a.warnings + result_b.warnings
    assert [w["reason"] for w in all_warnings] == ["supersede_target_not_transitionable"]

    pred_after = await real_get_document(pred.id)
    assert pred_after.lifecycle_status == "archived"
    inbound = await graph_store.get_edges_by_target(pred.id, "supersedes")
    assert len(inbound) == 1, f"the chain forked: {[(e.source_id, e.target_id) for e in inbound]}"
