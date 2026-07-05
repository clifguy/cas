"""Single-document purge orchestration (Tier A: in-memory stub stores).

Covers the dry-run/refusal/confirmation/cascade orchestration against the stub
stores. The real Postgres graph cascade (FK ordering, tag cascade) is covered by
``tests/sage/test_postgres_purge_ports.py::remove_document``.
"""

import pytest

from sage.maintenance.purge_document import purge_document
from sage.models.enums import TERMINAL_PIPELINE_STATUSES, PipelineStatus

_NONTERMINAL = [s for s in PipelineStatus if s not in TERMINAL_PIPELINE_STATUSES]


async def test_dry_run_enumerates_and_writes_nothing(
    stub_graph, stub_content, vault_dir, make_doc, make_edge, make_chunk, audit_records, capsys
):
    doc = make_doc("target")
    other = make_doc("other")
    for d in (doc, other):
        await stub_graph.insert_document(d)
    await stub_graph.insert_edge(make_edge(doc.id, other.id))
    await stub_content.index_chunks(doc.id, [make_chunk(doc.id, 0), make_chunk(doc.id, 1)])

    rc = await purge_document(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        document_id=doc.id,
        reason="r",
        apply=False,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "edges (outbound):        1" in out
    assert "content chunks:          2" in out
    assert "(dry-run; pass --apply to execute)" in out
    # Nothing removed, no audit written.
    assert await stub_graph.get_document(doc.id) is not None
    assert await stub_content.has_chunks(doc.id) is True
    assert audit_records() == []


async def test_refuses_unknown_document(stub_graph, stub_content, vault_dir, audit_records):
    rc = await purge_document(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        document_id="deadbeef_missing",
        reason="r",
        apply=True,
        input_fn=lambda _p: "deadbeef_missing",
    )
    assert rc == 2
    assert audit_records() == []


@pytest.mark.parametrize("as_source", [True, False], ids=["staging-as-source", "staging-as-target"])
async def test_refuses_when_staging_edge_references_target(
    as_source, stub_graph, stub_content, vault_dir, make_doc, make_staging, audit_records
):
    doc = make_doc("target")
    other = make_doc("other")
    for d in (doc, other):
        await stub_graph.insert_document(d)
    staging = make_staging(doc.id, other.id) if as_source else make_staging(other.id, doc.id)
    await stub_graph.insert_staging_edge(staging)

    rc = await purge_document(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        document_id=doc.id,
        reason="r",
        apply=True,
        input_fn=lambda _p: doc.id,
    )
    assert rc == 3
    assert await stub_graph.get_document(doc.id) is not None
    assert audit_records() == []


@pytest.mark.parametrize("status", _NONTERMINAL)
async def test_refuses_non_terminal_pipeline_status(
    status, stub_graph, stub_content, vault_dir, make_doc, audit_records
):
    doc = make_doc("target", pipeline_status=status)
    await stub_graph.insert_document(doc)

    rc = await purge_document(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        document_id=doc.id,
        reason="r",
        apply=True,
        input_fn=lambda _p: doc.id,
    )
    assert rc == 3
    assert await stub_graph.get_document(doc.id) is not None
    assert audit_records() == []


async def test_apply_wrong_confirmation_refuses(
    stub_graph, stub_content, vault_dir, make_doc, audit_records
):
    doc = make_doc("target")
    await stub_graph.insert_document(doc)

    rc = await purge_document(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        document_id=doc.id,
        reason="r",
        apply=True,
        input_fn=lambda _p: "not-the-id",
    )
    assert rc == 3
    assert await stub_graph.get_document(doc.id) is not None
    assert audit_records() == []


async def test_apply_removes_footprint_and_leaves_control(
    stub_graph, stub_content, vault_dir, make_doc, make_edge, make_chunk, audit_records
):
    target = make_doc("target")
    n1 = make_doc("n1")
    n2 = make_doc("n2")
    control = make_doc("control")
    for d in (target, n1, n2, control):
        await stub_graph.insert_document(d)
    await stub_graph.insert_edge(make_edge(target.id, n1.id))  # outbound
    await stub_graph.insert_edge(make_edge(n2.id, target.id))  # inbound
    await stub_graph.insert_edge(make_edge(control.id, n1.id))  # control's edge
    await stub_content.index_chunks(target.id, [make_chunk(target.id, 0), make_chunk(target.id, 1)])
    await stub_content.index_chunks(control.id, [make_chunk(control.id, 0)])

    rc = await purge_document(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        document_id=target.id,
        reason="r",
        apply=True,
        input_fn=lambda _p: target.id,
    )

    assert rc == 0
    assert await stub_graph.get_document(target.id) is None
    assert await stub_graph.get_edges_by_source(target.id) == []
    assert await stub_graph.get_edges_by_target(target.id) == []
    assert await stub_content.has_chunks(target.id) is False
    # Control document, its edge, and its chunk survive.
    assert await stub_graph.get_document(control.id) is not None
    control_edges = await stub_graph.get_edges_by_source(control.id)
    assert [e.target_id for e in control_edges] == [n1.id]
    assert await stub_content.has_chunks(control.id) is True


async def test_apply_writes_single_audit_record_and_appends(
    stub_graph, stub_content, vault_dir, make_doc, audit_records
):
    pre_existing = make_doc("pre")
    doc = make_doc("target")
    for d in (pre_existing, doc):
        await stub_graph.insert_document(d)

    # A pre-existing audit line the purge must not clobber.
    (vault_dir / ".maintenance_log.jsonl").write_text('{"operation": "prior"}\n')

    rc = await purge_document(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        document_id=doc.id,
        reason="wrong-vault",
        apply=True,
        input_fn=lambda _p: doc.id,
    )

    assert rc == 0
    records = audit_records()
    assert len(records) == 2  # prior line preserved + one new
    assert records[0] == {"operation": "prior"}
    new = records[1]
    assert new["operation"] == "purge_document"
    assert new["document_id"] == doc.id
    assert new["title"] == doc.title
    assert new["source_path"] == doc.source_path
    assert new["source_content_hash"] == doc.source_content_hash
    assert new["doc_type"] == doc.doc_type
    assert new["reason"] == "wrong-vault"
    # Single-document purge carries neither grouping id.
    assert "batch_id" not in new
    assert "chain_id" not in new
