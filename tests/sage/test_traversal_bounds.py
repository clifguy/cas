"""Bounded graph traversal, request-pool statement timeouts, and the document
update column allowlist.

The traversal walk refuses to extend a path through a document already on it,
caps the raw rows it returns, and the service reports a capped walk through
``TraverseResponse.truncated``. Request pools carry a ``statement_timeout``
startup setting, which the content-store ``optimize`` lifts for its own
long-running statements. The document UPDATE path accepts only columns the
documents table defines.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import Document, Edge, TraverseRequest, TraverseResponse
from sage.services import graph_ops as graph_ops_module
from sage.storage.postgres.pool import (
    PostgresConnectionParams,
    build_conn_kwargs,
    pool_from_conninfo,
)
from sage.storage.postgres.schema import DOCUMENT_COLUMNS
from tests.helpers.statement_timeout import TimedOutStatement

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _id(name: str) -> str:
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


def _make_doc(doc_id: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Doc {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        lifecycle_status="active",
        source_content_hash="sha256:" + hashlib.sha256(doc_id.encode()).hexdigest(),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )


async def _seed(graph_store, names: list[str], pairs: list[tuple[str, str]]) -> None:
    """Insert one document per name and one ``references`` edge per pair."""
    for name in names:
        await graph_store.insert_document(_make_doc(_id(name)))
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i, (src, tgt) in enumerate(pairs):
        await graph_store.insert_edge(
            Edge(
                id=str(uuid.uuid4()),
                source_id=_id(src),
                target_id=_id(tgt),
                edge_type=EdgeType.REFERENCES,
                created_at=base + timedelta(seconds=i),
            )
        )


def _all_pairs(names: list[str]) -> list[tuple[str, str]]:
    return [(a, b) for a in names for b in names if a != b]


def _expected_rows(start: str, pairs: list[tuple[str, str]], depth: int) -> int:
    """Rows a path-guarded outbound walk yields: one per simple path from
    ``start`` of length 1..depth, plus one per edge that closes a path back
    onto a document already on it (emitted, never extended)."""
    out: dict[str, list[str]] = {}
    for src, tgt in pairs:
        out.setdefault(src, []).append(tgt)
    count = 0

    def walk(node: str, path: list[str]) -> None:
        nonlocal count
        if len(path) - 1 >= depth:
            return
        for nxt in out.get(node, []):
            count += 1
            if nxt not in path:
                walk(nxt, [*path, nxt])

    walk(start, [start])
    return count


# ---------------------------------------------------------------------------
# Storage: path guard and row cap
# ---------------------------------------------------------------------------


async def test_deep_traverse_over_cross_linked_documents_is_bounded(graph_store):
    """Densely cross-linked documents: a deep walk finishes promptly and
    returns exactly the path-guarded row set."""
    linked = ["c1", "c2", "c3", "c4"]
    pairs = [("a", "b"), ("b", "a"), ("a", "c"), ("c", "a"), ("a", "c1"), *_all_pairs(linked)]
    await _seed(graph_store, ["a", "b", "c", *linked], pairs)

    rows = await asyncio.wait_for(
        graph_store.traverse(
            start_id=_id("a"), edge_type="references", direction="outbound", depth=50
        ),
        timeout=5.0,
    )
    assert len(rows) == _expected_rows("a", pairs, 50)
    # The closing edges stay visible: the start document is reached back.
    assert _id("a") in {r["doc_id"] for r in rows}


async def test_traverse_inbound_and_both_are_path_guarded(graph_store):
    names = ["p", "q", "r"]
    await _seed(graph_store, names, _all_pairs(names))
    pairs = _all_pairs(names)
    # Inbound walks the reversed edges; both walks every edge either way.
    expected = {
        "inbound": _expected_rows("p", [(t, s) for s, t in pairs], 50),
        "both": _expected_rows("p", pairs + [(t, s) for s, t in pairs], 50),
    }
    for direction, count in expected.items():
        rows = await asyncio.wait_for(
            graph_store.traverse(start_id=_id("p"), edge_type=None, direction=direction, depth=50),
            timeout=5.0,
        )
        assert len(rows) == count, direction


async def test_traverse_row_limit_returns_one_overflow_row(graph_store):
    """With a ``row_limit`` the store returns at most ``row_limit + 1`` rows; the
    extra row is the caller's truncation signal."""
    names = ["k1", "k2", "k3", "k4", "k5", "k6"]
    pairs = _all_pairs(names)
    await _seed(graph_store, names, pairs)
    limit = 50
    total = _expected_rows("k1", pairs, 5)
    assert total > limit + 1

    capped = await graph_store.traverse(
        start_id=_id("k1"), edge_type="references", direction="outbound", depth=5, row_limit=limit
    )
    assert len(capped) == limit + 1

    whole = await graph_store.traverse(
        start_id=_id("k1"),
        edge_type="references",
        direction="outbound",
        depth=5,
        row_limit=100_000,
    )
    assert len(whole) == total
    # Level-by-level evaluation: every level shallower than the deepest one the
    # capped walk reached is complete, so the cap dropped the deepest rows.
    deepest = max(r["depth"] for r in capped)
    for level in range(1, deepest):
        assert sum(r["depth"] == level for r in capped) == sum(
            r["depth"] == level for r in whole
        ), level


# ---------------------------------------------------------------------------
# Service: truncated flag and unchanged dedup semantics
# ---------------------------------------------------------------------------


async def test_service_cyclic_walk_reports_each_document_at_min_depth(
    graph_store, graph_ops_service
):
    linked = ["m1", "m2", "m3", "m4"]
    await _seed(graph_store, linked, _all_pairs(linked))
    result = await graph_ops_service.traverse(
        TraverseRequest(start_id=_id("m1"), edge_type=EdgeType.REFERENCES, depth=50)
    )
    assert result.truncated is False
    depths = {n.document.id: n.depth for n in result.nodes}
    assert depths == {_id("m1"): 2, _id("m2"): 1, _id("m3"): 1, _id("m4"): 1}


async def test_service_reports_truncation_when_the_row_cap_is_hit(
    graph_store, graph_ops_service, monkeypatch
):
    names = ["t1", "t2", "t3", "t4", "t5"]
    await _seed(graph_store, names, _all_pairs(names))
    monkeypatch.setattr(graph_ops_module, "TRAVERSE_ROW_CAP", 10)
    result = await graph_ops_service.traverse(
        TraverseRequest(start_id=_id("t1"), edge_type=EdgeType.REFERENCES, depth=5)
    )
    assert result.truncated is True


async def test_service_row_cap_is_shared_across_seeds_and_directions(
    graph_store, graph_ops_service, monkeypatch
):
    """``direction=both`` runs an outbound and an inbound walk; the cap is one
    budget across them, not one per walk."""
    names = ["s1", "s2", "s3"]
    await _seed(graph_store, names, _all_pairs(names))
    outbound_only = await graph_store.traverse(
        start_id=_id("s1"), edge_type="references", direction="outbound", depth=3
    )
    # A budget the outbound walk alone exhausts must report truncation for
    # both directions together.
    monkeypatch.setattr(graph_ops_module, "TRAVERSE_ROW_CAP", len(outbound_only))
    result = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("s1"), edge_type=EdgeType.REFERENCES, direction="both", depth=3
        )
    )
    assert result.truncated is True


def test_traverse_response_truncated_defaults_false():
    response = TraverseResponse(start_id=_id("x"), nodes=[])
    assert response.truncated is False
    assert response.model_dump()["truncated"] is False


@pytest.mark.parametrize("depth, ok", [(50, True), (51, False), (1000, False)])
def test_traverse_request_depth_ceiling(depth, ok):
    if ok:
        assert TraverseRequest(start_id=_id("x"), depth=depth).depth == depth
    else:
        with pytest.raises(ValueError):
            TraverseRequest(start_id=_id("x"), depth=depth)


# ---------------------------------------------------------------------------
# Request-pool statement timeout
# ---------------------------------------------------------------------------


def test_build_conn_kwargs_emits_statement_timeout():
    params = PostgresConnectionParams(search_path="v,public", statement_timeout_ms=60_000)
    kwargs = build_conn_kwargs(params, environ={})
    assert kwargs["options"] == "-c search_path=v,public -c statement_timeout=60000"


def test_build_conn_kwargs_statement_timeout_without_search_path():
    kwargs = build_conn_kwargs(PostgresConnectionParams(statement_timeout_ms=1500), environ={})
    assert kwargs["options"] == "-c statement_timeout=1500"


def test_build_conn_kwargs_omits_statement_timeout_by_default():
    kwargs = build_conn_kwargs(PostgresConnectionParams(search_path="v"), environ={})
    assert "statement_timeout" not in kwargs["options"]


def test_stack_config_statement_timeout_default_and_binding():
    from sage.config import StackPostgresConfig
    from sage.storage_binding import PostgresVaultStorageProvisioner

    assert StackPostgresConfig().statement_timeout_seconds == 60
    provisioner = PostgresVaultStorageProvisioner(StackPostgresConfig(statement_timeout_seconds=7))
    pool_params = provisioner._connection_params(search_path="v,public", request_pool=True)
    assert pool_params.statement_timeout_ms == 7000
    # Plain provisioning connections (schema bootstrap, drops) stay unbounded.
    assert provisioner._connection_params().statement_timeout_ms is None


def test_stack_config_statement_timeout_must_be_positive():
    from sage.config import StackPostgresConfig

    with pytest.raises(ValueError):
        StackPostgresConfig(statement_timeout_seconds=0)


async def test_pool_statement_timeout_cancels_a_long_statement(pg_dsn, pg_schema):
    pool = pool_from_conninfo(
        pg_dsn, search_path=f"{pg_schema},public", max_size=2, statement_timeout_ms=1000
    )
    await pool.open()
    try:
        async with pool.connection() as conn:
            cur = await conn.execute("SHOW statement_timeout")
            assert (await cur.fetchone())[0] == "1s"
            with pytest.raises(psycopg.errors.QueryCanceled):
                await conn.execute("SELECT pg_sleep(3)")
    finally:
        await pool.close()


async def test_optimize_lifts_the_statement_timeout_and_restores_it(pg_dsn, pg_schema, monkeypatch):
    from sage.adapters.content_store_postgres import PostgresContentStore

    pool = pool_from_conninfo(
        pg_dsn, search_path=f"{pg_schema},public", max_size=1, statement_timeout_ms=1000
    )
    await pool.open()
    try:
        store = PostgresContentStore(pool)
        seen: list[str] = []
        original = PostgresContentStore._bloat_snapshot

        async def spy(conn):
            cur = await conn.execute("SELECT current_setting('statement_timeout')")
            seen.append((await cur.fetchone())[0])
            return await original(conn)

        monkeypatch.setattr(PostgresContentStore, "_bloat_snapshot", staticmethod(spy))
        await store.optimize(timedelta(0))
        assert seen == ["0", "0"]
        async with pool.connection() as conn:
            cur = await conn.execute("SHOW statement_timeout")
            assert (await cur.fetchone())[0] == "1s"
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Document update column allowlist
# ---------------------------------------------------------------------------


async def test_update_document_refuses_an_unknown_column(graph_store):
    doc_id = _id("u1")
    await graph_store.insert_document(_make_doc(doc_id))
    with pytest.raises(ValueError, match="not_a_column"):
        await graph_store.update_document(doc_id, {"title": "x", "not_a_column": 1})
    stored = await graph_store.get_document(doc_id)
    assert stored.title == f"Doc {doc_id}"


async def test_update_document_accepts_known_columns(graph_store):
    doc_id = _id("u2")
    await graph_store.insert_document(_make_doc(doc_id))
    updated = await graph_store.update_document(doc_id, {"title": "renamed"})
    assert updated.title == "renamed"


async def test_document_columns_match_the_provisioned_table(pg_dsn, pg_schema):
    async with await psycopg.AsyncConnection.connect(pg_dsn) as conn:
        cur = await conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'documents'",
            (pg_schema,),
        )
        live = {row[0] for row in await cur.fetchall()}
    assert live == set(DOCUMENT_COLUMNS)


async def test_service_passes_the_remaining_budget_to_every_store_walk(
    graph_store, graph_ops_service, monkeypatch
):
    """The cap bounds the database walk itself, not only the response: every
    store call carries the budget still unspent."""
    names = ["w1", "w2", "w3"]
    await _seed(graph_store, names, _all_pairs(names))
    calls: list[int | None] = []
    original = graph_store.traverse

    async def recording(**kwargs):
        calls.append(kwargs.get("row_limit"))
        return await original(**kwargs)

    monkeypatch.setattr(graph_store, "traverse", recording)
    await graph_ops_service.traverse(
        TraverseRequest(
            start_id=_id("w1"), edge_type=EdgeType.REFERENCES, direction="both", depth=3
        )
    )
    assert len(calls) == 2
    assert calls[0] == graph_ops_module.TRAVERSE_ROW_CAP
    assert calls[1] is not None and calls[1] < calls[0]


@pytest.mark.parametrize(
    "method, probe",
    [
        ("migrate_indexed_structure", "_vector_is_current"),
        ("rebuild_document_surface_vector", "_surface_vector_is_current"),
    ],
)
async def test_migration_rewrites_lift_the_statement_timeout(
    pg_dsn, pg_schema, monkeypatch, method, probe
):
    """The migration transactions that may rewrite a whole table run without
    the pool's statement timeout, and the lift ends with the transaction."""
    from sage.adapters.content_store_postgres import PostgresContentStore

    pool = pool_from_conninfo(
        pg_dsn, search_path=f"{pg_schema},public", max_size=1, statement_timeout_ms=1000
    )
    await pool.open()
    try:
        store = PostgresContentStore(pool)
        seen: list[str] = []
        original = getattr(PostgresContentStore, probe)

        async def spy(conn):
            cur = await conn.execute("SELECT current_setting('statement_timeout')")
            seen.append((await cur.fetchone())[0])
            return await original(conn)

        monkeypatch.setattr(PostgresContentStore, probe, staticmethod(spy))
        if method == "migrate_indexed_structure":
            await store.migrate_indexed_structure([])
        else:
            await store.rebuild_document_surface_vector()
        assert seen == ["0"]
        async with pool.connection() as conn:
            cur = await conn.execute("SHOW statement_timeout")
            assert (await cur.fetchone())[0] == "1s"
    finally:
        await pool.close()


def test_stack_config_statement_timeout_upper_bound():
    """The ceiling is the largest whole-second value Postgres accepts."""
    from sage.config import StackPostgresConfig

    assert StackPostgresConfig(statement_timeout_seconds=2147483).statement_timeout_seconds
    with pytest.raises(ValueError):
        StackPostgresConfig(statement_timeout_seconds=2147484)


async def test_pool_accepts_the_largest_configurable_timeout(pg_dsn, pg_schema):
    pool = pool_from_conninfo(
        pg_dsn, search_path=f"{pg_schema},public", max_size=1, statement_timeout_ms=2147483 * 1000
    )
    await pool.open(wait=True, timeout=10)
    try:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT current_setting('statement_timeout')")
            assert (await cur.fetchone())[0] != "0"
    finally:
        await pool.close()


async def test_open_vault_storage_pools_carry_the_statement_timeout(pg_dsn, tmp_path):
    """The production entry point opens request pools with the configured
    timeout; the plain provisioning paths stay unbounded."""
    from psycopg.conninfo import conninfo_to_dict

    from sage.config import StackPostgresConfig
    from sage.storage_binding import PostgresVaultStorageProvisioner

    parsed = conninfo_to_dict(pg_dsn)
    provisioner = PostgresVaultStorageProvisioner(
        StackPostgresConfig(
            host=parsed.get("host"),
            port=int(parsed.get("port") or 5432),
            database=str(parsed.get("dbname")),
            user=parsed.get("user"),
            extensions=["vector", "pgstattuple"],
            statement_timeout_seconds=7,
        )
    )
    vault_id = f"bounds_{uuid.uuid4().hex[:8]}"
    handle = await provisioner.open_vault_storage(
        vault_id, tmp_path, need_graph=False, need_content=False
    )
    try:
        async with handle.pool.connection() as conn:
            cur = await conn.execute("SHOW statement_timeout")
            assert (await cur.fetchone())[0] == "7s"
    finally:
        await handle.pool.close()
        await provisioner.drop_vault_schema(vault_id)
    assert provisioner.purge_audit_sink(vault_id)._params.statement_timeout_ms is None


async def test_tier3_unique_index_build_lifts_the_statement_timeout(pg_dsn, pg_schema, monkeypatch):
    from sage.storage.postgres import graph_store as graph_store_module
    from sage.storage.postgres.graph_store import PostgresGraphStore

    probe = (
        "DO $$ BEGIN IF current_setting('statement_timeout') <> '0' THEN "
        "RAISE EXCEPTION 'bounded'; END IF; END $$"
    )
    monkeypatch.setattr(graph_store_module, "tier3_unique_index_ddl_pg", lambda d, f: probe)
    pool = pool_from_conninfo(
        pg_dsn, search_path=f"{pg_schema},public", max_size=1, statement_timeout_ms=1000
    )
    await pool.open()
    try:
        await PostgresGraphStore(pool).ensure_tier3_unique_index("ticket", "ticket_id")
        async with pool.connection() as conn:
            cur = await conn.execute("SHOW statement_timeout")
            assert (await cur.fetchone())[0] == "1s"
    finally:
        await pool.close()


def _recursive_union_rows(plan: dict) -> int:
    """Actual rows the recursive CTE produced, from an EXPLAIN ANALYZE plan."""
    if plan.get("Node Type") == "Recursive Union":
        return int(plan["Actual Rows"]) * int(plan.get("Actual Loops", 1))
    for child in plan.get("Plans", []):
        found = _recursive_union_rows(child)
        if found >= 0:
            return found
    return -1


async def test_row_limit_bounds_the_database_walk_itself(graph_store, monkeypatch):
    """The limit stops the recursion in the database rather than trimming a
    finished walk: the recursive CTE produces no more than ``row_limit + 1``
    rows even when the uncapped walk is far larger."""
    names = ["d1", "d2", "d3", "d4", "d5", "d6"]
    pairs = _all_pairs(names)
    await _seed(graph_store, names, pairs)
    limit = 20
    assert _expected_rows("d1", pairs, 6) > 10 * limit

    produced: list[int] = []
    original = graph_store._fetch_rows

    async def explaining(sql, params=()):
        async with graph_store._pool.connection() as conn:
            cur = await conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)
            plan = (await cur.fetchone())[0][0]["Plan"]
        produced.append(_recursive_union_rows(plan))
        return await original(sql, params)

    monkeypatch.setattr(graph_store, "_fetch_rows", explaining)
    rows = await graph_store.traverse(
        start_id=_id("d1"), edge_type="references", direction="outbound", depth=6, row_limit=limit
    )
    assert len(rows) == limit + 1
    assert produced and 0 <= produced[0] <= limit + 1


def test_published_depth_ceiling_matches_the_model():
    import yaml

    from sage.models.schemas import TraverseRequest as Model

    spec = yaml.safe_load(
        (_REPO_ROOT / "docs/fs/sage/sage_core_api.openapi.yaml").read_text(encoding="utf-8")
    )
    depth = spec["components"]["schemas"]["TraverseRequest"]["properties"]["depth"]
    bounds = {m.__class__.__name__: m for m in Model.model_fields["depth"].metadata}
    assert depth["maximum"] == bounds["Le"].le == 50
    assert depth["minimum"] == bounds["Ge"].ge


def test_published_statement_timeout_bounds_match_the_model():
    from sage.config import StackPostgresConfig

    schema = json.loads(
        (_REPO_ROOT / "docs/fs/sage/sage_core_config.schema.json").read_text(encoding="utf-8")
    )
    prop = schema["properties"]["postgres"]["properties"]["statement_timeout_seconds"]
    field = StackPostgresConfig.model_fields["statement_timeout_seconds"]
    bounds = {m.__class__.__name__: m for m in field.metadata}
    assert prop["default"] == field.default
    assert prop["minimum"] == bounds["Ge"].ge
    assert prop["maximum"] == bounds["Le"].le


@pytest.mark.parametrize(
    "method, table",
    [
        ("migrate_indexed_structure", "chunks"),
        ("rebuild_document_surface_vector", "document_surface"),
    ],
)
async def test_migration_lock_wait_is_not_bounded_by_the_pool_timeout(
    pg_dsn, pg_schema, method, table
):
    """A migration that queues behind another holder of its table lock waits
    past the pool's statement timeout instead of being cancelled."""
    from sage.adapters.content_store_postgres import PostgresContentStore

    pool = pool_from_conninfo(
        pg_dsn, search_path=f"{pg_schema},public", max_size=1, statement_timeout_ms=500
    )
    await pool.open()
    holder = await psycopg.AsyncConnection.connect(pg_dsn, options=f"-c search_path={pg_schema}")
    try:
        await holder.execute(f"LOCK TABLE {table} IN SHARE UPDATE EXCLUSIVE MODE")
        store = PostgresContentStore(pool)
        call = (
            store.migrate_indexed_structure([])
            if method == "migrate_indexed_structure"
            else store.rebuild_document_surface_vector()
        )
        task = asyncio.create_task(call)
        # Wait until the migration is queued on the lock, then hold it past
        # the pool's timeout before releasing.
        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as probe:
            for _ in range(100):
                cur = await probe.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND query LIKE %s",
                    (f"LOCK TABLE {table} %",),
                )
                if (await cur.fetchone())[0]:
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("the migration never queued on the table lock")
        await asyncio.sleep(1.0)
        await holder.commit()
        await asyncio.wait_for(task, timeout=10)
    finally:
        await holder.close()
        await pool.close()


async def test_traverse_does_not_extend_a_self_loop_on_the_start_document(graph_store):
    """An edge from the start document to itself is reported once and ends its
    path at the first step."""
    pairs = [("e1", "e1"), ("e1", "e2"), ("e2", "e1")]
    await _seed(graph_store, ["e1", "e2"], pairs)
    rows = await graph_store.traverse(
        start_id=_id("e1"), edge_type="references", direction="outbound", depth=5
    )
    assert len(rows) == _expected_rows("e1", pairs, 5)
    self_loop = [r for r in rows if r["source_id"] == r["target_id"]]
    assert [r["depth"] for r in self_loop] == [1]


# ---------------------------------------------------------------------------
# A cancelled statement reaches callers as ``statement_timeout``
# ---------------------------------------------------------------------------


async def test_a_pool_timeout_cancellation_maps_to_statement_timeout(pg_dsn, pg_schema):
    from sage.api.errors import statement_timeout_error

    pool = pool_from_conninfo(
        pg_dsn, search_path=f"{pg_schema},public", max_size=1, statement_timeout_ms=200
    )
    await pool.open()
    try:
        async with pool.connection() as conn:
            with pytest.raises(psycopg.errors.QueryCanceled) as caught:
                await conn.execute("SELECT pg_sleep(2)")
    finally:
        await pool.close()
    mapped = statement_timeout_error(caught.value)
    assert mapped is not None and (mapped.code, mapped.status_code) == ("statement_timeout", 503)
    try:
        raise RuntimeError("wrapped") from caught.value
    except RuntimeError as wrapped:
        assert statement_timeout_error(wrapped) is not None
    assert statement_timeout_error(RuntimeError("other")) is None


async def test_an_operator_cancel_is_not_reported_as_a_timeout(pg_dsn):
    """Another cancellation shares the SQLSTATE but not the cause, so it stays
    an unexpected error rather than ``statement_timeout``."""
    from sage.api.errors import statement_timeout_error

    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as victim:
        pid = victim.info.backend_pid

        async def cancel_soon() -> None:
            async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as admin:
                for _ in range(100):
                    cur = await admin.execute(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE pid = %s AND query LIKE 'SELECT pg_sleep%%'",
                        (pid,),
                    )
                    if (await cur.fetchone())[0]:
                        break
                    await asyncio.sleep(0.05)
                await admin.execute("SELECT pg_cancel_backend(%s)", (pid,))

        canceller = asyncio.create_task(cancel_soon())
        with pytest.raises(psycopg.errors.QueryCanceled) as caught:
            await victim.execute("SELECT pg_sleep(5)")
        await canceller
    assert caught.value.sqlstate == "57014"
    assert statement_timeout_error(caught.value) is None


@pytest.mark.parametrize("call", ["query_documents", "search_abstracts", "query_document_facets"])
async def test_catalog_query_lets_a_timeout_through_untranslated(pg_dsn, pg_schema, call):
    """A cancelled catalog query is a timeout, not a refused query, so it is
    not reported as ``storage_query_failed``."""
    from sage.adapters.interfaces import StorageQueryError
    from sage.storage.postgres.graph_store import PostgresGraphStore

    pool = pool_from_conninfo(
        pg_dsn, search_path=f"{pg_schema},public", max_size=1, statement_timeout_ms=300
    )
    await pool.open()
    holder = await psycopg.AsyncConnection.connect(pg_dsn, options=f"-c search_path={pg_schema}")
    try:
        await holder.execute("LOCK TABLE documents IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(psycopg.errors.QueryCanceled) as caught:
            store = PostgresGraphStore(pool)
            if call == "search_abstracts":
                await store.search_abstracts("anything")
            else:
                await getattr(store, call)()
        assert not isinstance(caught.value, StorageQueryError)
    finally:
        await holder.rollback()
        await holder.close()
        await pool.close()


def test_rest_answers_a_cancelled_statement_with_statement_timeout():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sage.api.errors import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/cancelled")
    async def cancelled() -> None:
        raise TimedOutStatement()

    @app.get("/broken")
    async def broken() -> None:
        raise RuntimeError("boom")

    client = TestClient(app, raise_server_exceptions=False)
    timed_out = client.get("/cancelled")
    assert timed_out.status_code == 503
    assert timed_out.json()["code"] == "statement_timeout"
    other = client.get("/broken")
    assert (other.status_code, other.text) == (500, "Internal Server Error")
    assert other.headers["content-type"].startswith("text/plain")


def test_mcp_answers_a_cancelled_statement_with_statement_timeout():
    from sage.mcp_server import _internal_error_payload

    payload = _internal_error_payload(TimedOutStatement(), "traverse")
    assert payload["error"] == "statement_timeout"
    assert _internal_error_payload(RuntimeError("boom"), "traverse")["error"] == "internal_error"
