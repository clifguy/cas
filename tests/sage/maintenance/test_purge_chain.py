"""Chain purge orchestration (Tier B: real Postgres graph store).

Chain membership resolution depends on real graph traversal (``chain_walk``,
head/linearity detection over a recursive CTE) that the in-memory stub
deliberately does not fake, so these run against the ``postgres_graph_store``
fixture (skips without ``SAGE_TEST_PG_DSN``). The content side uses the stub
content store; the graph cascade is the real thing.
"""

import pytest

from sage.maintenance.purge_chain import purge_chain
from sage.models.enums import EdgeType, PipelineStatus

pytest.importorskip("sage.storage.postgres.graph_store")


def _responder(*responses):
    """Return an ``input``-shaped callable yielding ``responses`` in order."""
    it = iter(responses)
    return lambda _prompt: next(it)


class _FailOne:
    """Wrap a graph store so ``remove_document`` fails for exactly one id."""

    def __init__(self, inner, fail_id: str) -> None:
        self._inner = inner
        self._fail_id = fail_id

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def remove_document(self, document_id: str) -> None:
        if document_id == self._fail_id:
            raise RuntimeError("simulated per-member failure")
        return await self._inner.remove_document(document_id)


async def _linear_chain(store, make_doc, make_edge):
    """Build v3 supersedes v2 supersedes v1; return (v1, v2, v3). Head is v3."""
    v1, v2, v3 = make_doc("v1"), make_doc("v2"), make_doc("v3")
    for d in (v1, v2, v3):
        await store.insert_document(d)
    await store.insert_edge(make_edge(v3.id, v2.id, EdgeType.SUPERSEDES))
    await store.insert_edge(make_edge(v2.id, v1.id, EdgeType.SUPERSEDES))
    return v1, v2, v3


async def test_dry_run_enumerates_linear_chain(
    postgres_graph_store, stub_content, stub_audit_sink, make_doc, make_edge, audit_records, capsys
):
    v1, v2, v3 = await _linear_chain(postgres_graph_store, make_doc, make_edge)

    rc = await purge_chain(
        graph_store=postgres_graph_store,
        content_store=stub_content,
        audit_sink=stub_audit_sink,
        head_id=v3.id,
        reason="r",
        apply=False,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "Chain length: 3" in out
    # Head-first order: v3 at position 0, v1 last.
    assert out.index(v3.id) < out.index(v2.id) < out.index(v1.id)
    for d in (v1, v2, v3):
        assert await postgres_graph_store.get_document(d.id) is not None
    assert audit_records() == []


async def test_refuses_unknown_head(postgres_graph_store, stub_content, stub_audit_sink):
    rc = await purge_chain(
        graph_store=postgres_graph_store,
        content_store=stub_content,
        audit_sink=stub_audit_sink,
        head_id="deadbeef_missing",
        reason="r",
        apply=False,
    )
    assert rc == 2


async def test_refuses_non_head_id(
    postgres_graph_store, stub_content, stub_audit_sink, make_doc, make_edge, capsys
):
    v1, v2, v3 = await _linear_chain(postgres_graph_store, make_doc, make_edge)

    rc = await purge_chain(
        graph_store=postgres_graph_store,
        content_store=stub_content,
        audit_sink=stub_audit_sink,
        head_id=v2.id,  # middle, not the head
        reason="r",
        apply=False,
    )

    assert rc == 3
    assert v3.id in capsys.readouterr().err


async def test_refuses_branched_without_flag_but_allows_with_flag(
    postgres_graph_store, stub_content, stub_audit_sink, make_doc, make_edge
):
    v3, v2a, v2b = make_doc("v3"), make_doc("v2a"), make_doc("v2b")
    for d in (v3, v2a, v2b):
        await postgres_graph_store.insert_document(d)
    await postgres_graph_store.insert_edge(make_edge(v3.id, v2a.id, EdgeType.SUPERSEDES))
    await postgres_graph_store.insert_edge(make_edge(v3.id, v2b.id, EdgeType.SUPERSEDES))

    refused = await purge_chain(
        graph_store=postgres_graph_store,
        content_store=stub_content,
        audit_sink=stub_audit_sink,
        head_id=v3.id,
        reason="r",
        apply=False,
    )
    assert refused == 3

    allowed = await purge_chain(
        graph_store=postgres_graph_store,
        content_store=stub_content,
        audit_sink=stub_audit_sink,
        head_id=v3.id,
        reason="r",
        apply=False,
        allow_branched=True,
    )
    assert allowed == 0


async def test_refuses_when_member_has_staging(
    postgres_graph_store,
    stub_content,
    stub_audit_sink,
    make_doc,
    make_edge,
    make_staging,
    audit_records,
):
    v1, v2, v3 = await _linear_chain(postgres_graph_store, make_doc, make_edge)
    other = make_doc("other")
    await postgres_graph_store.insert_document(other)
    await postgres_graph_store.insert_staging_edge(make_staging(v2.id, other.id))

    rc = await purge_chain(
        graph_store=postgres_graph_store,
        content_store=stub_content,
        audit_sink=stub_audit_sink,
        head_id=v3.id,
        reason="r",
        apply=True,
        input_fn=_responder(v3.id, "3"),
    )

    assert rc == 3
    for d in (v1, v2, v3):
        assert await postgres_graph_store.get_document(d.id) is not None
    assert audit_records() == []


async def test_refuses_when_member_non_terminal(
    postgres_graph_store, stub_content, stub_audit_sink, make_doc, make_edge, audit_records
):
    v1 = make_doc("v1")
    v2 = make_doc("v2", pipeline_status=PipelineStatus.INDEXING_IN_PROGRESS)
    v3 = make_doc("v3")
    for d in (v1, v2, v3):
        await postgres_graph_store.insert_document(d)
    await postgres_graph_store.insert_edge(make_edge(v3.id, v2.id, EdgeType.SUPERSEDES))
    await postgres_graph_store.insert_edge(make_edge(v2.id, v1.id, EdgeType.SUPERSEDES))

    rc = await purge_chain(
        graph_store=postgres_graph_store,
        content_store=stub_content,
        audit_sink=stub_audit_sink,
        head_id=v3.id,
        reason="r",
        apply=True,
        input_fn=_responder(v3.id, "3"),
    )

    assert rc == 3
    for d in (v1, v2, v3):
        assert await postgres_graph_store.get_document(d.id) is not None
    assert audit_records() == []


async def test_wrong_head_confirmation_refuses(
    postgres_graph_store, stub_content, stub_audit_sink, make_doc, make_edge, audit_records
):
    v1, v2, v3 = await _linear_chain(postgres_graph_store, make_doc, make_edge)

    rc = await purge_chain(
        graph_store=postgres_graph_store,
        content_store=stub_content,
        audit_sink=stub_audit_sink,
        head_id=v3.id,
        reason="r",
        apply=True,
        input_fn=_responder("wrong-head", "3"),
    )

    assert rc == 3
    assert await postgres_graph_store.get_document(v3.id) is not None
    assert audit_records() == []


async def test_wrong_length_confirmation_refuses(
    postgres_graph_store, stub_content, stub_audit_sink, make_doc, make_edge, audit_records
):
    v1, v2, v3 = await _linear_chain(postgres_graph_store, make_doc, make_edge)

    rc = await purge_chain(
        graph_store=postgres_graph_store,
        content_store=stub_content,
        audit_sink=stub_audit_sink,
        head_id=v3.id,
        reason="r",
        apply=True,
        input_fn=_responder(v3.id, "999"),
    )

    assert rc == 3
    assert await postgres_graph_store.get_document(v3.id) is not None
    assert audit_records() == []


async def test_apply_purges_all_members_sharing_chain_id(
    postgres_graph_store, stub_content, stub_audit_sink, make_doc, make_edge, audit_records
):
    v1, v2, v3 = await _linear_chain(postgres_graph_store, make_doc, make_edge)
    control = make_doc("control")
    await postgres_graph_store.insert_document(control)

    rc = await purge_chain(
        graph_store=postgres_graph_store,
        content_store=stub_content,
        audit_sink=stub_audit_sink,
        head_id=v3.id,
        reason="r",
        apply=True,
        input_fn=_responder(v3.id, "3"),
    )

    assert rc == 0
    for d in (v1, v2, v3):
        assert await postgres_graph_store.get_document(d.id) is None
    assert await postgres_graph_store.get_document(control.id) is not None

    records = audit_records()
    assert len(records) == 3
    chain_ids = {r["chain_id"] for r in records}
    assert len(chain_ids) == 1
    assert all(r["operation"] == "purge_chain" for r in records)
    assert all("batch_id" not in r for r in records)


async def test_halt_on_member_failure_preserves_prior_and_untouched(
    postgres_graph_store, stub_content, stub_audit_sink, make_doc, make_edge, audit_records
):
    v1, v2, v3 = await _linear_chain(postgres_graph_store, make_doc, make_edge)
    wrapped = _FailOne(postgres_graph_store, v2.id)  # middle member fails

    rc = await purge_chain(
        graph_store=wrapped,
        content_store=stub_content,
        audit_sink=stub_audit_sink,
        head_id=v3.id,
        reason="r",
        apply=True,
        input_fn=_responder(v3.id, "3"),
    )

    assert rc == 4
    assert await postgres_graph_store.get_document(v3.id) is None  # first purged
    assert await postgres_graph_store.get_document(v2.id) is not None  # failing member survives
    assert await postgres_graph_store.get_document(v1.id) is not None  # later untouched
    records = audit_records()
    assert {r["document_id"] for r in records} == {v3.id, v2.id}
    assert len({r["chain_id"] for r in records}) == 1
