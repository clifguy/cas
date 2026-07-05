"""Ingest-window batch purge orchestration (Tier A: in-memory stub stores).

The stub graph store implements ``find_documents_ingested_between`` with real
in-memory semantics, so window selection, whole-batch pre-flight rejection,
typed-count confirmation, and halt-on-failure all run without Postgres. The
half-open ``created_at`` boundary against a real store is covered by
``tests/sage/test_postgres_purge_ports.py``.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sage.adapters.stubs import StubGraphStore
from sage.maintenance.purge_batch import purge_batch
from sage.models.enums import PipelineStatus

_SINCE = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
_UNTIL = _SINCE + timedelta(minutes=5)


class _GraphFailsFor(StubGraphStore):
    """Stub graph store whose ``remove_document`` raises for one id."""

    def __init__(self, fail_id: str) -> None:
        super().__init__()
        self._fail_id = fail_id

    async def remove_document(self, document_id: str) -> None:
        if document_id == self._fail_id:
            raise RuntimeError("simulated per-document failure")
        await super().remove_document(document_id)


async def _seed_in_window(stub_graph, make_doc, count=3):
    """Insert ``count`` docs inside [_SINCE, _UNTIL), ordered by created_at."""
    docs = []
    for i in range(count):
        d = make_doc(f"in{i}", created_at=_SINCE + timedelta(minutes=i))
        await stub_graph.insert_document(d)
        docs.append(d)
    return docs


async def test_selector_window_is_half_open(stub_graph, stub_content, vault_dir, make_doc):
    """Apply purges only the in-window docs; the doc exactly at _UNTIL and the
    docs outside the window survive."""
    inside = await _seed_in_window(stub_graph, make_doc, count=3)
    before = make_doc("before", created_at=_SINCE - timedelta(minutes=1))
    at_until = make_doc("at_until", created_at=_UNTIL)
    after = make_doc("after", created_at=_UNTIL + timedelta(minutes=1))
    for d in (before, at_until, after):
        await stub_graph.insert_document(d)

    rc = await purge_batch(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        since=_SINCE,
        until=_UNTIL,
        reason="r",
        apply=True,
        input_fn=lambda _p: "3",
    )

    assert rc == 0
    for d in inside:
        assert await stub_graph.get_document(d.id) is None
    for d in (before, at_until, after):
        assert await stub_graph.get_document(d.id) is not None


async def test_until_none_defaults_to_now(stub_graph, stub_content, vault_dir, make_doc):
    """With ``until=None`` the upper bound is 'now', so a far-future doc is out of
    window and survives while a past doc is purged."""
    past = make_doc("past", created_at=datetime.now(timezone.utc) - timedelta(days=1))
    future = make_doc("future", created_at=datetime.now(timezone.utc) + timedelta(days=365))
    for d in (past, future):
        await stub_graph.insert_document(d)

    rc = await purge_batch(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        since=datetime.now(timezone.utc) - timedelta(days=2),
        until=None,
        reason="r",
        apply=True,
        input_fn=lambda _p: "1",
    )

    assert rc == 0
    assert await stub_graph.get_document(past.id) is None
    assert await stub_graph.get_document(future.id) is not None


async def test_empty_window_zero_targets_no_audit(
    stub_graph, stub_content, vault_dir, make_doc, audit_records, capsys
):
    out_of_window = make_doc("x", created_at=_UNTIL + timedelta(hours=1))
    await stub_graph.insert_document(out_of_window)

    rc = await purge_batch(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        since=_SINCE,
        until=_UNTIL,
        reason="r",
        apply=True,
        input_fn=lambda _p: "0",
    )

    assert rc == 0
    assert "(no documents in window; nothing to do)" in capsys.readouterr().out
    assert audit_records() == []


async def test_dry_run_enumerates_and_writes_nothing(
    stub_graph, stub_content, vault_dir, make_doc, make_chunk, audit_records, capsys
):
    docs = await _seed_in_window(stub_graph, make_doc, count=2)
    await stub_content.index_chunks(docs[0].id, [make_chunk(docs[0].id, 0)])
    await stub_content.index_chunks(
        docs[1].id, [make_chunk(docs[1].id, 0), make_chunk(docs[1].id, 1)]
    )

    rc = await purge_batch(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        since=_SINCE,
        until=_UNTIL,
        reason="r",
        apply=False,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "Target count: 2" in out
    assert "3 content chunks cumulative" in out
    assert "(dry-run; pass --apply to execute)" in out
    for d in docs:
        assert await stub_graph.get_document(d.id) is not None
    assert audit_records() == []


@pytest.mark.parametrize("typed", ["", "yes", "3 ", "03", "2"])
async def test_typed_count_confirmation_strict_refuses(
    typed, stub_graph, stub_content, vault_dir, make_doc, audit_records
):
    docs = await _seed_in_window(stub_graph, make_doc, count=3)

    rc = await purge_batch(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        since=_SINCE,
        until=_UNTIL,
        reason="r",
        apply=True,
        input_fn=lambda _p: typed,
    )

    assert rc == 3
    for d in docs:
        assert await stub_graph.get_document(d.id) is not None
    assert audit_records() == []


async def test_typed_count_confirmation_exact_proceeds(
    stub_graph, stub_content, vault_dir, make_doc
):
    docs = await _seed_in_window(stub_graph, make_doc, count=3)

    rc = await purge_batch(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        since=_SINCE,
        until=_UNTIL,
        reason="r",
        apply=True,
        input_fn=lambda _p: "3",
    )

    assert rc == 0
    for d in docs:
        assert await stub_graph.get_document(d.id) is None


async def test_preflight_rejects_when_any_target_has_staging(
    stub_graph, stub_content, vault_dir, make_doc, make_staging, audit_records
):
    docs = await _seed_in_window(stub_graph, make_doc, count=3)
    other = make_doc("other")
    await stub_graph.insert_document(other)
    await stub_graph.insert_staging_edge(make_staging(docs[1].id, other.id))

    rc = await purge_batch(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        since=_SINCE,
        until=_UNTIL,
        reason="r",
        apply=True,
        input_fn=lambda _p: "3",
    )

    assert rc == 3
    for d in docs:
        assert await stub_graph.get_document(d.id) is not None
    assert audit_records() == []


async def test_preflight_rejects_when_any_target_non_terminal(
    stub_graph, stub_content, vault_dir, make_doc, audit_records
):
    good = [
        await _insert(stub_graph, make_doc(f"g{i}", created_at=_SINCE + timedelta(minutes=i)))
        for i in range(2)
    ]
    bad = await _insert(
        stub_graph,
        make_doc(
            "bad",
            created_at=_SINCE + timedelta(minutes=2),
            pipeline_status=PipelineStatus.INDEXING_IN_PROGRESS,
        ),
    )

    rc = await purge_batch(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        since=_SINCE,
        until=_UNTIL,
        reason="r",
        apply=True,
        input_fn=lambda _p: "3",
    )

    assert rc == 3
    for d in (*good, bad):
        assert await stub_graph.get_document(d.id) is not None
    assert audit_records() == []


async def test_audit_one_entry_per_target_sharing_batch_id(
    stub_graph, stub_content, vault_dir, make_doc, audit_records
):
    docs = await _seed_in_window(stub_graph, make_doc, count=3)

    await purge_batch(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        since=_SINCE,
        until=_UNTIL,
        reason="r",
        apply=True,
        input_fn=lambda _p: "3",
    )

    records = audit_records()
    assert len(records) == 3
    batch_ids = {r["batch_id"] for r in records}
    assert len(batch_ids) == 1
    assert all(r["operation"] == "purge_batch" for r in records)
    assert {r["document_id"] for r in records} == {d.id for d in docs}


async def test_two_batches_have_distinct_batch_ids(
    stub_graph, stub_content, vault_dir, make_doc, audit_records
):
    first = await _insert(stub_graph, make_doc("a", created_at=_SINCE + timedelta(minutes=1)))
    await purge_batch(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        since=_SINCE,
        until=_UNTIL,
        reason="r",
        apply=True,
        input_fn=lambda _p: "1",
    )
    second = await _insert(
        stub_graph, make_doc("b", created_at=_SINCE + timedelta(days=1, minutes=1))
    )
    await purge_batch(
        graph_store=stub_graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        since=_SINCE + timedelta(days=1),
        until=_UNTIL + timedelta(days=1),
        reason="r",
        apply=True,
        input_fn=lambda _p: "1",
    )

    records = audit_records()
    assert {r["document_id"] for r in records} == {first.id, second.id}
    assert len({r["batch_id"] for r in records}) == 2


async def test_halt_on_failure_preserves_prior_and_untouched(
    stub_content, vault_dir, make_doc, audit_records
):
    docs = [make_doc(f"in{i}", created_at=_SINCE + timedelta(minutes=i)) for i in range(3)]
    graph = _GraphFailsFor(docs[1].id)  # the middle target (by created_at) fails
    for d in docs:
        await graph.insert_document(d)

    rc = await purge_batch(
        graph_store=graph,
        content_store=stub_content,
        vault_dir=vault_dir,
        since=_SINCE,
        until=_UNTIL,
        reason="r",
        apply=True,
        input_fn=lambda _p: "3",
    )

    assert rc == 4
    assert await graph.get_document(docs[0].id) is None  # first purged
    assert await graph.get_document(docs[1].id) is not None  # failing member survives
    assert await graph.get_document(docs[2].id) is not None  # later target untouched
    records = audit_records()
    # First (removed) and the failing member (audit-before-delete) are logged;
    # the untouched later target is not.
    assert {r["document_id"] for r in records} == {docs[0].id, docs[1].id}
    assert len({r["batch_id"] for r in records}) == 1


async def _insert(stub_graph, doc):
    await stub_graph.insert_document(doc)
    return doc
