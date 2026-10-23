"""Atomic admissions return their own snapshot before newer owners can publish."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import pytest

import sage.services.ingestion as ingestion_module
import sage.storage.postgres.graph_store as graph_module
from sage.adapters.content_store_postgres import PostgresContentStore
from sage.instrumentation.performance import scope
from sage.models.enums import EdgeType
from sage.models.schemas import IngestRequest
from sage.services.ingestion import IngestionService
from tests.helpers.pipeline_wait import drain_abstraction_queue
from tests.sage.test_postgres_graph_store import _doc, _edge


def _after_commit(monkeypatch, callback):
    observed = graph_module.observed_context
    owner = asyncio.current_task()
    fired = False

    @asynccontextmanager
    async def gate(manager, enter, exit, failure):
        nonlocal fired
        async with observed(manager, enter, exit, failure) as value:
            yield value
        if exit == "graph.commit" and asyncio.current_task() is owner and not fired:
            fired = True
            await callback()

    monkeypatch.setattr(graph_module, "observed_context", gate)


@pytest.mark.parametrize("race", [False, True])
@pytest.mark.parametrize("inline", [False, True])
async def test_supersede_keeps_its_own_admission_while_a_newer_owner_runs(
    ingestion_service, pg_pool, tmp_vault_dir, monkeypatch, race, inline
):
    old = ingestion_service
    graph = old._store
    old._content_store = PostgresContentStore(pg_pool)
    source = tmp_vault_dir / "predecessor.md"
    source.write_text("PREDECESSOR body")
    predecessor = (await old.ingest(IngestRequest(source=str(source)))).document
    source = tmp_vault_dir / "successor.md"
    source.write_text("SUCCESSOR OLD PIPELINE body")
    newer = IngestionService(
        graph,
        old._locks,
        PostgresContentStore(pg_pool),
        old._embedding,
        old._abstraction,
        old._config,
        source_adapters=old._adapters,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    child, snapshots = [], []
    insert = graph._exec_insert_document
    project, embed = ingestion_module._run_projection, newer._embed_chunks

    async def capture(conn, doc, **kwargs):
        await insert(conn, doc, **kwargs)
        child.append(doc.id)

    async def new_projection(*args, **kwargs):
        result = await project(*args, **kwargs)
        result.text = result.text.replace("OLD PIPELINE", "NEW OWNER")
        return result

    async def hold(chunks):
        assert any("NEW OWNER" in c.content for c in chunks)
        entered.set()
        await release.wait()
        return await embed(chunks)

    async def race_after_commit():
        assert len(child) == 1
        if race:
            monkeypatch.setattr(ingestion_module, "_run_projection", new_projection)
            await newer.recompute_pipeline(child[0])
            await asyncio.wait_for(entered.wait(), 5)
            snapshots.append(await graph.get_document(child[0]))

    monkeypatch.setattr(graph, "_exec_insert_document", capture)
    monkeypatch.setattr(newer, "_embed_chunks", hold)
    _after_commit(monkeypatch, race_after_commit)
    result = None
    try:
        try:
            result = await old.ingest(
                IngestRequest(source=str(source), predecessor_id=predecessor.id),
                wait_for_pipeline=inline,
            )
            await drain_abstraction_queue(old)
        except ingestion_module._SupersededProcessing:
            pass
        assert len(child) == 1
        if race:
            assert entered.is_set() and len(snapshots) == 1
            after = await graph.get_document(child[0])
            assert after == snapshots[0]
            assert after.processing_coverage.attempt == 2
            pending_chunks = await old._content_store.get_all_chunks(child[0])
            assert [c.content for c in pending_chunks] == ["SUCCESSOR NEW OWNER body"]
            assert after.processing_coverage.projection.generation == 2
            assert not await old._content_store.read_embedding_vectors(child[0], [0])
            release.set()
            await drain_abstraction_queue(newer)
            settled = await graph.get_document(child[0])
            assert settled.pipeline_status.value == "abstraction_complete"
            assert (
                settled.processing_coverage.projection.generation
                == snapshots[0].processing_coverage.attempt
            )
            assert [c.content for c in await old._content_store.get_all_chunks(child[0])] == [
                "SUCCESSOR NEW OWNER body"
            ]
        else:
            assert result is not None and result.document.id == child[0]
            settled = await graph.get_document(child[0])
            assert settled.pipeline_status.value == "abstraction_complete"
            assert settled.processing_coverage.projection.generation == 1
            assert [c.content for c in await old._content_store.get_all_chunks(child[0])] == [
                "SUCCESSOR OLD PIPELINE body"
            ]
    finally:
        release.set()
        await drain_abstraction_queue(newer)
        await old.stop_worker()
        await newer.stop_worker()


@pytest.mark.parametrize("race", [False, True])
async def test_compound_return_contains_both_rows_after_edge_effects_from_its_own_commit(
    postgres_graph_store, pg_pool, monkeypatch, race
):
    graph = postgres_graph_store
    predecessor, child = _doc(1), _doc(2)
    await graph.insert_document(predecessor)
    before = await graph.get_document(predecessor.id)
    async with pg_pool.connection() as conn:
        await conn.execute(
            "CREATE FUNCTION admission_edge_effects() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN UPDATE documents SET title = 'CHILD AT COMMIT' WHERE id = NEW.source_id; "
            "UPDATE documents SET title = 'PRED AT COMMIT' WHERE id = NEW.target_id; "
            "RETURN NEW; END $$"
        )
        await conn.execute(
            "CREATE TRIGGER admission_edge_effects AFTER INSERT ON edges "
            "FOR EACH ROW EXECUTE FUNCTION admission_edge_effects()"
        )
    later = []

    async def after_commit():
        if race:
            later.append(
                await graph.update_document(
                    child.id, {"title": "LATER CHILD"}, processing_stage="abstraction"
                )
            )
            later.append(
                await graph.update_document(
                    predecessor.id, {"title": "LATER PRED", "tags": ["later"]}
                )
            )

    _after_commit(monkeypatch, after_commit)
    try:
        inserted, updated = await graph.insert_with_supersede_atomic(
            child,
            predecessor.id,
            {"lifecycle_status": "archived", "tags": ["committed"]},
            _edge(child.id, predecessor.id, 1, EdgeType.SUPERSEDES),
            processing_stage="indexing",
        )
        assert inserted.id == child.id and updated.id == predecessor.id
        assert inserted.title == "CHILD AT COMMIT" and updated.title == "PRED AT COMMIT"
        assert inserted.processing_coverage.attempt == 1
        async with pg_pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, is_chain_head FROM documents WHERE id IN (%s, %s)",
                (child.id, predecessor.id),
            )
            assert dict(await cur.fetchall()) == {child.id: True, predecessor.id: False}
        assert updated.lifecycle_status == "archived" and updated.tags == ["committed"]
        assert int(updated.version_token) == int(before.version_token) + 1
        if race:
            assert len(later) == 2 and later[0].processing_coverage.attempt == 2
            assert (await graph.get_document(child.id)).title == "LATER CHILD"
            assert (await graph.get_document(predecessor.id)).title == "LATER PRED"
    finally:
        async with pg_pool.connection() as conn:
            await conn.execute("DROP TRIGGER admission_edge_effects ON edges")
            await conn.execute("DROP FUNCTION admission_edge_effects()")


@pytest.mark.parametrize("boundary", ["tags", "edge", "commit"])
async def test_compound_admission_withholds_return_and_rolls_back_late_failure(
    postgres_graph_store, pg_pool, caplog, boundary
):
    from psycopg.errors import RaiseException

    graph = postgres_graph_store
    predecessor, child = _doc(1), _doc(2)
    await graph.insert_document(predecessor)
    before = await graph.get_document(predecessor.id)
    table = "document_tags" if boundary == "tags" else "edges"
    async with pg_pool.connection() as conn:
        await conn.execute(
            "CREATE FUNCTION refuse_compound_admission() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'compound admission refused'; END $$"
        )
        if boundary == "commit":
            await conn.execute(
                "CREATE CONSTRAINT TRIGGER refuse_compound_admission AFTER INSERT ON edges "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                "EXECUTE FUNCTION refuse_compound_admission()"
            )
        else:
            # The predecessor's tag replacement is earlier than child insertion.
            when = f" WHEN (NEW.document_id = '{child.id}')" if boundary == "tags" else ""
            await conn.execute(
                f"CREATE TRIGGER refuse_compound_admission BEFORE INSERT ON {table} "
                f"FOR EACH ROW{when} EXECUTE FUNCTION refuse_compound_admission()"
            )
    returned = None
    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    try:
        with scope("vault", child.id, "admission"):
            with pytest.raises(RaiseException, match="compound admission refused"):
                returned = await graph.insert_with_supersede_atomic(
                    child,
                    predecessor.id,
                    {"lifecycle_status": "archived", "tags": ["committed"]},
                    _edge(child.id, predecessor.id, 1, EdgeType.SUPERSEDES),
                    processing_stage="indexing",
                )
        assert returned is None
        assert await graph.get_document(child.id) is None
        assert await graph.get_document(predecessor.id) == before
        assert await graph.get_edges_by_source(child.id) == []
        async with pg_pool.connection() as conn:
            tags = await conn.execute(
                "SELECT count(*) FROM document_tags WHERE document_id = %s", (child.id,)
            )
            assert (await tags.fetchone())[0] == 0
        [row] = [
            json.loads(r.message) for r in caplog.records if r.name == "sage.ingestion.performance"
        ]
        if boundary == "commit":
            assert row["stages"]["graph.commit"]["failed"] == 1
        else:
            assert row["stages"]["graph.rollback"]["count"] == 1
            assert "graph.commit" not in row["stages"]
    finally:
        async with pg_pool.connection() as conn:
            await conn.execute(f"DROP TRIGGER refuse_compound_admission ON {table}")
            await conn.execute("DROP FUNCTION refuse_compound_admission()")
