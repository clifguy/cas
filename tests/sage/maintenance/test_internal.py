"""The shared per-document purge primitive and the chain-shape helpers.

``_purge_one`` is audit-first and coordinates two single-store removals with no
cross-store atomicity (CAS-ADR-042); its result object must make a partial
failure fully traceable. The chain-shape helpers are storage-agnostic and are
unit-tested directly on the ``{"documents", "edges"}`` dict shape that
``GraphStore.chain_walk`` returns.
"""

from sage.adapters.stubs import StubContentStore, StubGraphStore
from sage.maintenance._internal import (
    _chain_head_ids,
    _chain_is_linear,
    _order_chain_from_head,
    _purge_one,
)


class _GraphRemoveFails(StubGraphStore):
    """Stub graph store whose ``remove_document`` always raises."""

    async def remove_document(self, document_id: str) -> None:
        raise RuntimeError("simulated graph failure")


class _ContentRemoveFails(StubContentStore):
    """Stub content store whose ``remove_document`` always raises."""

    async def remove_document(self, document_id: str) -> None:
        raise RuntimeError("simulated content failure")


# ---------------------------------------------------------------------------
# _purge_one: audit shape and grouping ids
# ---------------------------------------------------------------------------


async def test_audit_carries_batch_id_when_supplied(
    stub_graph, stub_content, vault_dir, make_doc, audit_records
):
    doc = make_doc("d")
    await stub_graph.insert_document(doc)

    result = await _purge_one(
        document_id=doc.id,
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        reason="r",
        operation="purge_batch",
        batch_id="BATCH-1",
    )

    assert result.succeeded
    (record,) = audit_records()
    assert record["batch_id"] == "BATCH-1"
    assert "chain_id" not in record


async def test_audit_carries_chain_id_when_supplied(
    stub_graph, stub_content, vault_dir, make_doc, audit_records
):
    doc = make_doc("d")
    await stub_graph.insert_document(doc)

    await _purge_one(
        document_id=doc.id,
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        reason="r",
        operation="purge_chain",
        chain_id="CHAIN-1",
    )

    (record,) = audit_records()
    assert record["chain_id"] == "CHAIN-1"
    assert "batch_id" not in record


async def test_audit_omits_grouping_ids_when_none(
    stub_graph, stub_content, vault_dir, make_doc, audit_records
):
    """A ``None`` grouping id must be absent from the record, not JSON null."""
    doc = make_doc("d")
    await stub_graph.insert_document(doc)

    await _purge_one(
        document_id=doc.id,
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        reason="r",
        operation="purge_document",
    )

    (record,) = audit_records()
    assert "batch_id" not in record
    assert "chain_id" not in record


async def test_missing_document_returns_failure_and_writes_no_audit(
    stub_graph, stub_content, vault_dir, audit_records
):
    result = await _purge_one(
        document_id="deadbeef_missing",
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        reason="r",
        operation="purge_document",
    )
    assert result.succeeded is False
    assert result.audit_written is False
    assert audit_records() == []


# ---------------------------------------------------------------------------
# _purge_one: audit-first + weakest-binding partial-failure traceability
# ---------------------------------------------------------------------------


async def test_audit_written_before_graph_delete(stub_content, vault_dir, make_doc, audit_records):
    """When the graph cascade fails, the audit record is already on disk and the
    result reports graph_committed=False -- the worst case is audit-with-no-delete,
    never delete-with-no-audit."""
    graph = _GraphRemoveFails()
    doc = make_doc("d")
    await graph.insert_document(doc)

    result = await _purge_one(
        document_id=doc.id,
        graph_store=graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        reason="r",
        operation="purge_document",
    )

    assert result.succeeded is False
    assert result.audit_written is True
    assert result.graph_committed is False
    assert result.content_removed is False
    (record,) = audit_records()
    assert record["document_id"] == doc.id
    # The document row survives the failed cascade.
    assert await graph.get_document(doc.id) is not None


async def test_content_failure_after_graph_success_is_traceable(
    stub_graph, vault_dir, make_doc, audit_records
):
    """Graph removed, content removal fails: the result distinguishes this from a
    total failure (graph_committed=True, content_removed=False) so an operator can
    see the chunks are orphaned. No cross-store atomicity is assumed."""
    content = _ContentRemoveFails()
    doc = make_doc("d")
    await stub_graph.insert_document(doc)

    result = await _purge_one(
        document_id=doc.id,
        graph_store=stub_graph,
        content_store=content,
        vault_dir=vault_dir,
        reason="r",
        operation="purge_document",
    )

    assert result.succeeded is False
    assert result.graph_committed is True
    assert result.content_removed is False
    assert result.audit_written is True
    assert await stub_graph.get_document(doc.id) is None
    assert len(audit_records()) == 1


# ---------------------------------------------------------------------------
# Chain-shape helpers (pure)
# ---------------------------------------------------------------------------

_LINEAR_DOCS = [{"doc_id": "v1"}, {"doc_id": "v2"}, {"doc_id": "v3"}]
# supersedes: source is newer -> v3 supersedes v2 supersedes v1; head = v3.
_LINEAR_EDGES = [
    {"source_id": "v3", "target_id": "v2"},
    {"source_id": "v2", "target_id": "v1"},
]

_BRANCHED_DOCS = [{"doc_id": "v3"}, {"doc_id": "v2a"}, {"doc_id": "v2b"}]
_BRANCHED_EDGES = [
    {"source_id": "v3", "target_id": "v2a"},
    {"source_id": "v3", "target_id": "v2b"},
]


def test_chain_head_ids_linear_is_single_newest():
    assert _chain_head_ids(_LINEAR_DOCS, _LINEAR_EDGES) == ["v3"]


def test_chain_is_linear_true_for_linear():
    assert _chain_is_linear(_LINEAR_DOCS, _LINEAR_EDGES) is True


def test_chain_is_linear_false_for_branched():
    assert _chain_is_linear(_BRANCHED_DOCS, _BRANCHED_EDGES) is False


def test_order_chain_from_head_is_head_first():
    assert _order_chain_from_head(_LINEAR_DOCS, _LINEAR_EDGES, "v3") == ["v3", "v2", "v1"]
