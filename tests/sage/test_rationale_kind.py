"""tests: rationale_kind helper and column wiring.

Covers T1 (helper round-trip). Write-path coverage lives in
tests/app/test_batch_ingest_service.py and tests/sage/test_mcp_server.py.

CAS-ADR-019.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sage.models.enums import (
    EdgeType,
    PipelineStatus,
    RationaleKind,
    ResolutionPolicy,
    SourceType,
    TraversalDirection,
)
from sage.models.schemas import Document, LinkRequest, TraverseRequest

# T1 ----------------------------------------------------------------------


def test_t1_derive_rationale_kind_round_trip():
    """T1. ``derive_rationale_kind`` returns the correct kind for each
    known prefix in ``RATIONALE_PREFIX_TO_KIND``, and ``'manual'`` for
    unprefixed input or ``None``.

    The helper is the single source of truth for the prefix-to-kind map.
    Without this round-trip guarantee the gate in CAS-ADR-019 silently
    misclassifies edges.
    """
    from sage.storage.edge_provenance import (
        RATIONALE_PREFIX_TO_KIND,
        derive_rationale_kind,
    )

    # Every documented prefix derives back to its declared kind.
    assert RATIONALE_PREFIX_TO_KIND, "prefix map must not be empty"
    for prefix, kind in RATIONALE_PREFIX_TO_KIND.items():
        assert isinstance(kind, RationaleKind)
        rationale = f"{prefix} some evidence text"
        assert derive_rationale_kind(rationale) is kind, (
            f"prefix {prefix!r} must derive to {kind!r}"
        )

    # Unprefixed and None fall through to MANUAL.
    assert derive_rationale_kind("hand-curated reason") is RationaleKind.MANUAL
    assert derive_rationale_kind("") is RationaleKind.MANUAL
    assert derive_rationale_kind(None) is RationaleKind.MANUAL

    # The literal prefix bracket must be at the start to count; embedded
    # prefix substrings must not match (defends against accidental
    # substring matches in handwritten rationale).
    one_prefix = next(iter(RATIONALE_PREFIX_TO_KIND))
    assert derive_rationale_kind(f"something then {one_prefix} after") is RationaleKind.MANUAL


# T10 ---------------------------------------------------------------------


def _id(name: str) -> str:
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


def _sha(name: str) -> str:
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


async def test_t10_traverse_returns_stored_rationale_kind(graph_store, graph_ops_service):
    """T10. ``traverse`` surfaces the stored ``rationale_kind`` on
    each edge rather than defaulting to ``manual``.

    Also asserts the parallel fields that ``_traverse_sync``'s row dict
    carries but that the ``GraphOpsService.traverse`` Edge() construction
    has historically dropped: ``resolution_policy``,
    ``source_valid_from_version``, and ``target_valid_from_version``.
    Without this regression guard, traversal-returned edges silently look
    like ``policy=none, no anchors`` regardless of what is stored. F4
    parallel sites covered here so future remediations don't drain the
    set one entry at a time.

    Two fields populated by the same fix are NOT asserted here:
      - ``valid_until_version``: requires a tombstone scenario (a
        merged_from edge tombstoning a predecessor); the field defaults
        to None on a fresh edge, so an ``is None`` assertion would not
        discriminate between the bug and the fix. Coverage deferred to
        a future merged_from-aware traversal test.
      - ``retracted_edge_id``: only non-null on a retracts edge, which
        has a different shape (target_id null, retracted_edge_id
        required). Coverage deferred to a future retracts-aware
        traversal test.
    Both fields are still populated by the production fix; only the
    test coverage is deferred.
    """
    src, tgt = _id("t10_src"), _id("t10_tgt")
    await graph_store.insert_document(_make_doc(src))
    await graph_store.insert_document(_make_doc(tgt))

    await graph_ops_service._create_edge_strict(
        LinkRequest(
            source_id=src,
            target_id=tgt,
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=src,
            target_valid_from_version=tgt,
            rationale_kind=RationaleKind.REFERENCES_MENTION,
        )
    )

    out = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=src,
            edge_type=EdgeType.REFERENCES,
            direction=TraversalDirection.OUTBOUND,
            depth=1,
        )
    )

    assert [n.document.id for n in out.nodes] == [tgt]
    out_edge = out.nodes[0].edge
    assert out_edge.rationale_kind is RationaleKind.REFERENCES_MENTION
    assert out_edge.resolution_policy is ResolutionPolicy.TRANSITIVE_BOTH
    assert out_edge.source_valid_from_version == src
    assert out_edge.target_valid_from_version == tgt

    inbound = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=tgt,
            edge_type=EdgeType.REFERENCES,
            direction=TraversalDirection.INBOUND,
            depth=1,
        )
    )
    assert [n.document.id for n in inbound.nodes] == [src]
    in_edge = inbound.nodes[0].edge
    assert in_edge.rationale_kind is RationaleKind.REFERENCES_MENTION
    assert in_edge.resolution_policy is ResolutionPolicy.TRANSITIVE_BOTH
    assert in_edge.source_valid_from_version == src
    assert in_edge.target_valid_from_version == tgt
