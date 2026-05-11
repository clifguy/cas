"""resolution_path debug payload tests (CAS-ADR-017, Chunk 7).

Covers TEST-SAGE-CR-037..042. Reuses the canonical ADR worked-example
setup (chains A/B + covers edge) and extends it for retracts and
merged_from scenarios where appropriate.

The debug payload is opt-in: when `TraverseRequest.debug=False` the
response's `resolution_path` is None (no per-event collection cost).
When `debug=True`, the resolver emits one entry per decision
(anchor_hit, anchor_miss, retracts_applied, tombstone_applied).
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sage.models.enums import (
    EdgeType,
    PipelineStatus,
    ResolutionPolicy,
    SourceType,
    TraversalDirection,
)
from sage.models.schemas import Document, Edge, LinkRequest, TraverseRequest


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "a1" or "doc_a"; this helper wraps them so the values still
    construct valid LinkRequest / TraverseRequest / ChainRequest
    instances. Deterministic — the same name always yields the same id.
    """
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


def _make_doc(doc_id: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Doc {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        source_content_hash=f"hash_{doc_id}",
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )


async def _seed_docs(graph_store, *doc_ids: str) -> None:
    for doc_id in doc_ids:
        await graph_store.insert_document(_make_doc(doc_id))


async def _seed_supersedes_chain(graph_store, chain: list[str]) -> None:
    now = datetime.now(timezone.utc)
    for i in range(1, len(chain)):
        newer, older = chain[i], chain[i - 1]
        await graph_store.insert_edge(
            Edge(
                id=str(uuid.uuid4()),
                source_id=newer,
                target_id=older,
                edge_type=EdgeType.SUPERSEDES,
                resolution_policy=ResolutionPolicy.NONE,
                created_at=now + timedelta(seconds=i),
            )
        )


async def _seed_ab_worked_example(graph_store, graph_ops_service) -> str:
    """Chain A (a1..a5), Chain B (b1..b3), covers edge at a3/b2."""
    chain_a = [_id("a1"), _id("a2"), _id("a3"), _id("a4"), _id("a5")]
    chain_b = [_id("b1"), _id("b2"), _id("b3")]
    await _seed_docs(graph_store, *chain_a, *chain_b)
    await _seed_supersedes_chain(graph_store, chain_a)
    await _seed_supersedes_chain(graph_store, chain_b)
    covers = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("a3"),
            target_id=_id("b2"),
            edge_type=EdgeType.COVERS,
            source_valid_from_version=_id("a3"),
            target_valid_from_version=_id("b2"),
        )
    )
    return covers.id


# ---------------------------------------------------------------------------
# CR-037: debug=false produces no resolution_path field
# ---------------------------------------------------------------------------


async def test_cr_037_debug_false_produces_no_resolution_path(graph_store, graph_ops_service):
    await _seed_ab_worked_example(graph_store, graph_ops_service)

    # Default (debug defaulted to false).
    out = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("a5"),
            edge_type=EdgeType.COVERS,
            direction=TraversalDirection.OUTBOUND,
            depth=2,
        )
    )
    assert out.resolution_path is None

    # Explicit debug=False.
    out_explicit = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("a5"),
            edge_type=EdgeType.COVERS,
            direction=TraversalDirection.OUTBOUND,
            depth=2,
            debug=False,
        )
    )
    assert out_explicit.resolution_path is None


# ---------------------------------------------------------------------------
# CR-038: debug=true records anchor_hit on surfaced edge
# ---------------------------------------------------------------------------


async def test_cr_038_debug_records_anchor_hit_on_surfaced_edge(graph_store, graph_ops_service):
    covers_id = await _seed_ab_worked_example(graph_store, graph_ops_service)

    out = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("a5"),
            edge_type=EdgeType.COVERS,
            direction=TraversalDirection.OUTBOUND,
            depth=2,
            debug=True,
        )
    )

    assert out.resolution_path is not None
    hits = [
        e for e in out.resolution_path if e.event_type == "anchor_hit" and e.edge_id == covers_id
    ]
    # transitive_both emits one anchor_hit per anchor check.
    fields = {e.anchor_field for e in hits}
    assert fields == {"source_valid_from_version", "target_valid_from_version"}
    source_hit = next(e for e in hits if e.anchor_field == "source_valid_from_version")
    assert source_hit.anchor_version == _id("a3")
    target_hit = next(e for e in hits if e.anchor_field == "target_valid_from_version")
    assert target_hit.anchor_version == _id("b2")


# ---------------------------------------------------------------------------
# CR-039: debug=true records anchor_miss on suppressed edge
# ---------------------------------------------------------------------------


async def test_cr_039_debug_records_anchor_miss_on_suppressed_edge(graph_store, graph_ops_service):
    """anchor_miss fires when the stored anchor sits outside its endpoint's
    supersedes lineage. The write-time validator prevents that state for
    edges created via `link`, so the path is exercised by inserting a
    malformed edge directly (simulating a legacy row or data corruption).
    """
    chain_a = [_id("a1"), _id("a2"), _id("a3")]
    chain_b = [_id("b1"), _id("b2")]
    await _seed_docs(graph_store, *chain_a, *chain_b)
    await _seed_supersedes_chain(graph_store, chain_a)
    await _seed_supersedes_chain(graph_store, chain_b)

    # Malformed edge: source_valid_from_version='a99' is not in
    # lineage(source_id='a3'). Bypass the write-time validator by
    # inserting the row directly.
    bad_edge = Edge(
        id="bad_covers",
        source_id=_id("a3"),
        target_id=_id("b2"),
        edge_type=EdgeType.COVERS,
        resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
        source_valid_from_version=_id("a99"),
        target_valid_from_version=_id("b2"),
        created_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_edge(bad_edge)

    out = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("a3"),
            edge_type=EdgeType.COVERS,
            direction=TraversalDirection.OUTBOUND,
            depth=2,
            debug=True,
        )
    )

    # Suppressed: anchor check fails.
    assert out.nodes == []
    assert out.resolution_path is not None
    misses = [
        e for e in out.resolution_path if e.event_type == "anchor_miss" and e.edge_id == bad_edge.id
    ]
    assert len(misses) == 1
    assert misses[0].anchor_field == "source_valid_from_version"
    assert misses[0].anchor_version == _id("a99")


# ---------------------------------------------------------------------------
# CR-040: debug=true records retracts_applied when a retraction suppresses
# ---------------------------------------------------------------------------


async def test_cr_040_debug_records_retracts_applied(graph_store, graph_ops_service):
    # Extended chain-A to a8 so we can anchor a retract at a7.
    chain_a = [
        _id("a1"),
        _id("a2"),
        _id("a3"),
        _id("a4"),
        _id("a5"),
        _id("a6"),
        _id("a7"),
        _id("a8"),
    ]
    chain_b = [_id("b1"), _id("b2"), _id("b3")]
    await _seed_docs(graph_store, *chain_a, *chain_b)
    await _seed_supersedes_chain(graph_store, chain_a)
    await _seed_supersedes_chain(graph_store, chain_b)
    covers = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("a3"),
            target_id=_id("b2"),
            edge_type=EdgeType.COVERS,
            source_valid_from_version=_id("a3"),
            target_valid_from_version=_id("b2"),
        )
    )
    retracts_edge = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("a7"),
            target_id=None,
            edge_type=EdgeType.RETRACTS,
            retracted_edge_id=covers.id,
            source_valid_from_version=_id("a7"),
        )
    )

    out = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("a8"),
            edge_type=EdgeType.COVERS,
            direction=TraversalDirection.OUTBOUND,
            depth=2,
            debug=True,
        )
    )

    assert out.nodes == []
    assert out.resolution_path is not None
    retracts_events = [
        e
        for e in out.resolution_path
        if e.event_type == "retracts_applied" and e.edge_id == covers.id
    ]
    assert len(retracts_events) == 1
    assert retracts_events[0].retracted_edge_id == retracts_edge.id


# ---------------------------------------------------------------------------
# CR-041: debug=true records tombstone_applied when a merge tombstones
# ---------------------------------------------------------------------------


async def test_cr_041_debug_records_tombstone_applied(graph_store, graph_ops_service):
    # Build chains A (a1..a8) and B (b1..b4); covers edge at a3/b2.
    chain_a = [
        _id("a1"),
        _id("a2"),
        _id("a3"),
        _id("a4"),
        _id("a5"),
        _id("a6"),
        _id("a7"),
        _id("a8"),
    ]
    chain_b = [_id("b1"), _id("b2"), _id("b3"), _id("b4")]
    await _seed_docs(graph_store, *chain_a, *chain_b)
    await _seed_supersedes_chain(graph_store, chain_a)
    await _seed_supersedes_chain(graph_store, chain_b)
    covers = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("a3"),
            target_id=_id("b2"),
            edge_type=EdgeType.COVERS,
            source_valid_from_version=_id("a3"),
            target_valid_from_version=_id("b2"),
        )
    )

    # c1 merged_from a8: tombstones the covers edge with valid_until=a8.
    await _seed_docs(graph_store, _id("c1"))
    await graph_ops_service.link(
        LinkRequest(
            source_id=_id("c1"),
            target_id=_id("a8"),
            edge_type=EdgeType.MERGED_FROM,
        )
    )

    # Append a hypothetical a9 post-merge so a8 is a strict ancestor on
    # chain A. A query from a9 is downstream of the merge termination:
    # tombstone suppresses the covers edge (strict-ancestor arm).
    await _seed_docs(graph_store, _id("a9"))
    await _seed_supersedes_chain(graph_store, [_id("a8"), _id("a9")])

    out = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("a9"),
            edge_type=EdgeType.COVERS,
            direction=TraversalDirection.OUTBOUND,
            depth=2,
            debug=True,
        )
    )

    assert out.nodes == []
    assert out.resolution_path is not None
    tombstone_events = [
        e
        for e in out.resolution_path
        if e.event_type == "tombstone_applied" and e.edge_id == covers.id
    ]
    assert len(tombstone_events) == 1
    assert tombstone_events[0].tombstone_version == _id("a8")


# ---------------------------------------------------------------------------
# CR-042: resolution_path preserves event order
# ---------------------------------------------------------------------------


async def test_cr_042_resolution_path_preserves_event_order(graph_store, graph_ops_service):
    """Mixed scenario: one covers edge that passes anchor (anchor_hit)
    plus one covers edge that fails the target anchor (anchor_miss).
    The anchor-filter phase runs before retracts/tombstone phases, so
    anchor events must precede any later-phase events.
    """
    # Chain A (a1..a5) + Chain B (b1..b3).
    chain_a = [_id("a1"), _id("a2"), _id("a3"), _id("a4"), _id("a5")]
    chain_b = [_id("b1"), _id("b2"), _id("b3")]
    await _seed_docs(graph_store, *chain_a, *chain_b)
    await _seed_supersedes_chain(graph_store, chain_a)
    await _seed_supersedes_chain(graph_store, chain_b)

    # Chain C (c1..c2) for a second covers edge; add a retract to mix
    # event types on this one.
    chain_c = [_id("c1"), _id("c2")]
    await _seed_docs(graph_store, *chain_c)
    await _seed_supersedes_chain(graph_store, chain_c)

    await graph_ops_service.link(
        LinkRequest(
            source_id=_id("a3"),
            target_id=_id("b2"),
            edge_type=EdgeType.COVERS,
            source_valid_from_version=_id("a3"),
            target_valid_from_version=_id("b2"),
        )
    )
    covers_ac = await graph_ops_service.link(
        LinkRequest(
            source_id=_id("a3"),
            target_id=_id("c1"),
            edge_type=EdgeType.COVERS,
            source_valid_from_version=_id("a3"),
            target_valid_from_version=_id("c1"),
        )
    )

    # Retract covers_ac anchored at a3 (in lineage of any a-chain query).
    await graph_ops_service.link(
        LinkRequest(
            source_id=_id("a3"),
            target_id=None,
            edge_type=EdgeType.RETRACTS,
            retracted_edge_id=covers_ac.id,
            source_valid_from_version=_id("a3"),
        )
    )

    out = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("a5"),
            edge_type=EdgeType.COVERS,
            direction=TraversalDirection.OUTBOUND,
            depth=2,
            debug=True,
        )
    )

    assert out.resolution_path is not None
    # covers_ab should surface (anchor_hit events); covers_ac should be
    # suppressed by the retract (anchor_hit events then retracts_applied).
    assert {n.document.id for n in out.nodes} == {_id("b2")}

    # Event ordering invariant: for covers_ac, the retracts_applied
    # event must follow any anchor_hit/anchor_miss events for that edge.
    ac_events = [e for e in out.resolution_path if e.edge_id == covers_ac.id]
    assert any(e.event_type == "retracts_applied" for e in ac_events)
    retract_idx = next(i for i, e in enumerate(ac_events) if e.event_type == "retracts_applied")
    # Every anchor-phase event for this edge precedes the retract event.
    for i, e in enumerate(ac_events):
        if e.event_type in ("anchor_hit", "anchor_miss"):
            assert i < retract_idx

    # Global phase invariant: the last anchor-phase event (across all
    # edges) appears before the first retracts_applied event.
    anchor_indices = [
        i
        for i, e in enumerate(out.resolution_path)
        if e.event_type in ("anchor_hit", "anchor_miss")
    ]
    retract_indices = [
        i for i, e in enumerate(out.resolution_path) if e.event_type == "retracts_applied"
    ]
    if anchor_indices and retract_indices:
        assert max(anchor_indices) < min(retract_indices)
