"""Retracts end-to-end resolver tests (CAS-ADR-017, Chunk 5).

Covers TEST-SAGE-CR-023..028. Uses the canonical ADR worked example
extended with a retracting chain long enough to anchor at a7:

    Chain A: a1 <- a2 <- a3 <- a4 <- a5 <- a6 <- a7 <- a8
    Chain B: b1 <- b2 <- b3
    covers: source=a3, target=b2, policy=transitive_both
    retracts: source=a7, retracted_edge_id=<covers>, source_anchor=a7
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
        await graph_store.insert_edge(Edge(
            id=str(uuid.uuid4()),
            source_id=newer,
            target_id=older,
            edge_type=EdgeType.SUPERSEDES,
            resolution_policy=ResolutionPolicy.NONE,
            created_at=now + timedelta(seconds=i),
        ))


async def _seed_retract_setup(
    graph_store, graph_ops_service, retract_anchor: str | None = None
):
    if retract_anchor is None:
        retract_anchor = _id("a7")
    """Canonical example plus a retracts edge anchored at `retract_anchor`."""
    chain_a = [_id("a1"), _id("a2"), _id("a3"), _id("a4"), _id("a5"), _id("a6"), _id("a7"), _id("a8")]
    chain_b = [_id("b1"), _id("b2"), _id("b3")]
    await _seed_docs(graph_store, *chain_a, *chain_b)
    await _seed_supersedes_chain(graph_store, chain_a)
    await _seed_supersedes_chain(graph_store, chain_b)
    covers = await graph_ops_service.link(LinkRequest(
        source_id=_id("a3"),
        target_id=_id("b2"),
        edge_type=EdgeType.COVERS,
        source_valid_from_version=_id("a3"),
        target_valid_from_version=_id("b2"),
    ))
    retracts_edge = await graph_ops_service.link(LinkRequest(
        source_id=retract_anchor,
        target_id=None,
        edge_type=EdgeType.RETRACTS,
        retracted_edge_id=covers.id,
        source_valid_from_version=retract_anchor,
    ))
    return covers, retracts_edge


# ---------------------------------------------------------------------------
# CR-023: retracts at a7 suppresses covers at query (a8, *)
# ---------------------------------------------------------------------------

async def test_cr_023_retracts_suppresses_downstream_of_anchor(
    graph_store, graph_ops_service
):
    await _seed_retract_setup(graph_store, graph_ops_service)

    out = await graph_ops_service.traverse(TraverseRequest(
        start_id=_id("a8"),
        edge_type=EdgeType.COVERS,
        direction=TraversalDirection.OUTBOUND,
        depth=2,
    ))
    assert out.nodes == []


# ---------------------------------------------------------------------------
# CR-024: retracts at a7 does not suppress covers at query (a6, *)
# ---------------------------------------------------------------------------

async def test_cr_024_retracts_does_not_suppress_upstream_of_anchor(
    graph_store, graph_ops_service
):
    await _seed_retract_setup(graph_store, graph_ops_service)

    out = await graph_ops_service.traverse(TraverseRequest(
        start_id=_id("a6"),
        edge_type=EdgeType.COVERS,
        direction=TraversalDirection.OUTBOUND,
        depth=2,
    ))
    assert [n.document.id for n in out.nodes] == [_id("b2")]


# ---------------------------------------------------------------------------
# CR-025: retracts does not affect queries from the counterpart chain
# ---------------------------------------------------------------------------

async def test_cr_025_retracts_is_one_sided(
    graph_store, graph_ops_service
):
    await _seed_retract_setup(graph_store, graph_ops_service)

    inbound = await graph_ops_service.traverse(TraverseRequest(
        start_id=_id("b3"),
        edge_type=EdgeType.COVERS,
        direction=TraversalDirection.INBOUND,
        depth=2,
    ))
    assert [n.document.id for n in inbound.nodes] == [_id("a3")]


# ---------------------------------------------------------------------------
# CR-026: multiple retracts of the same edge — first in lineage wins
# ---------------------------------------------------------------------------

async def test_cr_026_multiple_retracts_any_in_lineage_suppresses(
    graph_store, graph_ops_service
):
    covers, _ = await _seed_retract_setup(graph_store, graph_ops_service)
    # Add a second retracts anchored at a5.
    await graph_ops_service.link(LinkRequest(
        source_id=_id("a5"),
        target_id=None,
        edge_type=EdgeType.RETRACTS,
        retracted_edge_id=covers.id,
        source_valid_from_version=_id("a5"),
    ))

    # a8 has both a5 and a7 in its lineage -> suppressed.
    out_a8 = await graph_ops_service.traverse(TraverseRequest(
        start_id=_id("a8"),
        edge_type=EdgeType.COVERS,
        direction=TraversalDirection.OUTBOUND,
        depth=2,
    ))
    assert out_a8.nodes == []

    # a6 has a5 in lineage but not a7 -> suppressed by the a5 retract.
    out_a6 = await graph_ops_service.traverse(TraverseRequest(
        start_id=_id("a6"),
        edge_type=EdgeType.COVERS,
        direction=TraversalDirection.OUTBOUND,
        depth=2,
    ))
    assert out_a6.nodes == []

    # a4 has neither a5 nor a7 in lineage -> surfaces.
    out_a4 = await graph_ops_service.traverse(TraverseRequest(
        start_id=_id("a4"),
        edge_type=EdgeType.COVERS,
        direction=TraversalDirection.OUTBOUND,
        depth=2,
    ))
    assert [n.document.id for n in out_a4.nodes] == [_id("b2")]


# ---------------------------------------------------------------------------
# CR-027: retracts edge itself is traversable (policy none)
# ---------------------------------------------------------------------------

async def test_cr_027_retracts_edge_itself_traversable(
    graph_store, graph_ops_service
):
    _, retracts_edge = await _seed_retract_setup(graph_store, graph_ops_service)

    # Outbound traverse from a7 for edge_type=retracts. The retracts edge
    # has target_id=None so the traversal joins on documents via target_id
    # and will not surface a node (the edge points at another edge, not a
    # document). We assert the edge exists and has the expected shape
    # (introspection via storage layer) rather than via traverse, since
    # traverse is document-oriented.
    edges = await graph_store.get_edges_by_source(_id("a7"), EdgeType.RETRACTS.value)
    assert len(edges) == 1
    assert edges[0].id == retracts_edge.id
    assert edges[0].resolution_policy == ResolutionPolicy.NONE
    assert edges[0].target_id is None
    assert edges[0].retracted_edge_id is not None


# ---------------------------------------------------------------------------
# CR-028: retracts of a policy=none edge - permitted, lineage fact only
# ---------------------------------------------------------------------------

async def test_cr_028_retracts_of_supersedes_edge_does_not_affect_chain(
    graph_store, graph_ops_service
):
    chain_a = [_id("a1"), _id("a2"), _id("a3"), _id("a4"), _id("a5")]
    await _seed_docs(graph_store, *chain_a)
    await _seed_supersedes_chain(graph_store, chain_a)

    # Identify a supersedes edge (a3 -> a2) and retract it from a4.
    a3_supersedes = await graph_store.get_edges_by_source(
        _id("a3"), edge_type="supersedes"
    )
    supersedes_edge = next(
        e for e in a3_supersedes if e.target_id == _id("a2")
    )
    assert supersedes_edge is not None

    await graph_ops_service.link(LinkRequest(
        source_id=_id("a4"),
        target_id=None,
        edge_type=EdgeType.RETRACTS,
        retracted_edge_id=supersedes_edge.id,
        source_valid_from_version=_id("a4"),
    ))

    # Supersedes traversal from a5 should still reach the full chain. The
    # retracts primitive does not veto policy=none edges.
    out = await graph_ops_service.traverse(TraverseRequest(
        start_id=_id("a5"),
        edge_type=EdgeType.SUPERSEDES,
        direction=TraversalDirection.OUTBOUND,
        depth=5,
    ))
    assert {n.document.id for n in out.nodes} == {_id("a1"), _id("a2"), _id("a3"), _id("a4")}


# ---------------------------------------------------------------------------
# CR-053: retracts suppression applies to transitive_target edges
# ---------------------------------------------------------------------------

from sage.models.edge_registry import EdgeTypeRegistry  # noqa: E402
from sage.services.graph_ops import GraphOpsService  # noqa: E402


async def test_cr_053_retracts_suppresses_transitive_target_edge(
    graph_store, minimal_config
):
    """Retracts must suppress transitive_target edges the same way it
    suppresses transitive_source / transitive_both: when the retract
    anchor is in the query start's supersedes lineage.

    Setup: transitive_target edge from a3 (frozen source) to b2; retract
    anchored at a3 on the same (source) chain. Baseline: with no
    retract, outbound from a3 surfaces b2. With the retract in force,
    outbound from a3 is empty.
    """
    registry = EdgeTypeRegistry({
        EdgeType.SUPERSEDES: ResolutionPolicy.NONE,
        EdgeType.RETRACTS: ResolutionPolicy.NONE,
        EdgeType.MERGED_FROM: ResolutionPolicy.NONE,
        EdgeType.DERIVED_FROM: ResolutionPolicy.TRANSITIVE_SOURCE,
        EdgeType.INSTANTIATED_FROM: ResolutionPolicy.TRANSITIVE_BOTH,
        EdgeType.REFERENCES: ResolutionPolicy.TRANSITIVE_BOTH,
        EdgeType.COVERS: ResolutionPolicy.TRANSITIVE_BOTH,
        EdgeType.BUNDLES_WITH: ResolutionPolicy.TRANSITIVE_BOTH,
        EdgeType.DEPENDS_ON: ResolutionPolicy.TRANSITIVE_BOTH,
        EdgeType.AUTHORITATIVE_FOR: ResolutionPolicy.TRANSITIVE_TARGET,
        EdgeType.SYNC_TARGET: ResolutionPolicy.TBD,
    })
    service = GraphOpsService(
        graph_store, minimal_config, edge_type_registry=registry
    )

    await _seed_docs(graph_store, _id("a3"), _id("b1"), _id("b2"))
    await _seed_supersedes_chain(graph_store, [_id("b1"), _id("b2")])

    auth = await service.link(LinkRequest(
        source_id=_id("a3"),
        target_id=_id("b2"),
        edge_type=EdgeType.AUTHORITATIVE_FOR,
        target_valid_from_version=_id("b2"),
    ))

    # Baseline: without a retract, the edge surfaces outbound from a3.
    baseline = await service.traverse(TraverseRequest(
        start_id=_id("a3"),
        edge_type=EdgeType.AUTHORITATIVE_FOR,
        direction=TraversalDirection.OUTBOUND,
        depth=2,
    ))
    assert [n.document.id for n in baseline.nodes] == [_id("b2")]

    # Retract anchored at a3: retract.source_valid_from_version=a3 sits
    # in the lineage of query start a3.
    await service.link(LinkRequest(
        source_id=_id("a3"),
        target_id=None,
        edge_type=EdgeType.RETRACTS,
        retracted_edge_id=auth.id,
        source_valid_from_version=_id("a3"),
    ))

    # Suppression fires: the transitive_target edge is filtered out.
    suppressed = await service.traverse(TraverseRequest(
        start_id=_id("a3"),
        edge_type=EdgeType.AUTHORITATIVE_FOR,
        direction=TraversalDirection.OUTBOUND,
        depth=2,
    ))
    assert suppressed.nodes == []
