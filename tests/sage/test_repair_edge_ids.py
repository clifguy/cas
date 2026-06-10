"""Tests for scripts/repair_edge_ids.py — the edge-id repair script.

The script re-mints ``edges`` and ``staging_edges`` ids that violate the
UUID contract (rows written before boundary validation existed — hand
repairs, historical imports) in place, preserving every other column
byte-for-byte, and cascades ``retracted_edge_id`` references on the
edges table. Such rows can only be staged below the validated API, so
fixtures write them with raw SQL against a production-shaped store.

These tests drive the ``run`` entry point under dry-run (default) and
``apply`` modes and verify the JSON audit trail, the post-run row
state, and that strict read surfaces accept the store afterward.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sage.migration.vault_to_postgres import _collect_edges
from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import Document, Edge
from scripts.repair_edge_ids import main, run
from tests.sage.test_graph_store import _id, _sha

_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_USER = str(uuid.uuid4())


def _db(tmp_vault_dir: Path) -> Path:
    return tmp_vault_dir / "brain" / "graph.db"


def _make_doc(doc_id: str) -> Document:
    return Document(
        id=doc_id,
        title=f"Doc {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"imports/{doc_id}.md",
        source_content_hash=_sha(doc_id),
        adapter_version="0.5.0",
        created_by=_USER,
        created_at=_NOW,
        last_modified_by=_USER,
        updated_at=_NOW,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )


async def _seed_docs(store, *names: str) -> list[str]:
    ids = [_id(n) for n in names]
    for doc_id in ids:
        await store.insert_document(_make_doc(doc_id))
    return ids


async def _insert_valid_edge(
    store, source_id: str, target_id: str | None, edge_type: EdgeType, **kw
) -> str:
    edge_id = str(uuid.uuid4())
    await store.insert_edge(
        Edge(
            id=edge_id,
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            created_at=_NOW,
            **kw,
        )
    )
    return edge_id


def _insert_raw(db_path: Path, table: str, row: dict) -> None:
    """Insert a row bypassing all validation (FK enforcement off by default)."""
    cols = list(row)
    placeholders = ",".join("?" for _ in cols)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",  # noqa: S608 -- test fixture; trusted literals
            [row[c] for c in cols],
        )
        conn.commit()
    finally:
        conn.close()


def _raw_edge(
    db_path: Path, edge_id: str, source_id: str, target_id: str | None, edge_type: str, **extra
) -> None:
    row = {
        "id": edge_id,
        "source_id": source_id,
        "target_id": target_id,
        "edge_type": edge_type,
        "created_at": _NOW.isoformat(),
        "rationale_kind": "manual",
        **extra,
    }
    _insert_raw(db_path, "edges", row)


def _raw_staging(
    db_path: Path, edge_id: str, source_id: str, target_id: str, edge_type: str = "references"
) -> None:
    _insert_raw(
        db_path,
        "staging_edges",
        {
            "id": edge_id,
            "source_id": source_id,
            "target_id": target_id,
            "edge_type": edge_type,
            "inference_evidence": "evidence",
            "confidence_tier": 2,
            "created_at": _NOW.isoformat(),
        },
    )


def _all_rows(db_path: Path, table: str) -> dict[str, dict]:
    """Full column snapshot keyed by id, for byte-identical comparisons."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 -- test fixture; trusted literal
        return {r["id"]: dict(r) for r in rows}
    finally:
        conn.close()


def _parse_audit(capsys) -> tuple[list[dict], dict]:
    """Split captured stdout into (entry lines, the single summary line)."""
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.strip()]
    entries = [rec for rec in lines if "old_id" in rec]
    summaries = [rec for rec in lines if "totals" in rec]
    assert len(summaries) == 1, f"expected exactly one summary line, got {len(summaries)}"
    return entries, summaries[0]


# ---------------------------------------------------------------------------
# T1
# ---------------------------------------------------------------------------


async def test_clean_store_scans_all_and_repairs_nothing(sqlite_graph_store, tmp_vault_dir, capsys):
    """A store with only valid ids is scanned in full and left untouched.

    Trap: the scanned total must be 1, not 0 — an implementation that
    "passes" by never scanning reports zero.
    """
    src, tgt = await _seed_docs(sqlite_graph_store, "doc_src", "doc_tgt")
    await _insert_valid_edge(sqlite_graph_store, src, tgt, EdgeType.REFERENCES)
    before = _all_rows(_db(tmp_vault_dir), "edges")

    rc = run(_db(tmp_vault_dir), apply=True)

    entries, summary = _parse_audit(capsys)
    assert rc == 0
    assert entries == []
    assert summary["totals"]["edges"]["scanned"] == 1
    assert summary["totals"]["edges"]["malformed"] == 0
    assert _all_rows(_db(tmp_vault_dir), "edges") == before


# ---------------------------------------------------------------------------
# T2
# ---------------------------------------------------------------------------


async def test_dry_run_reports_malformed_row_and_mutates_nothing(
    sqlite_graph_store, tmp_vault_dir, capsys
):
    """Dry-run (default) reports the malformed row and changes no byte.

    Trap: the byte-identical snapshot comparison catches an
    implementation that repairs during dry-run, or one that classifies
    by attempting the UPDATE and rolling back imperfectly.
    """
    src, tgt = await _seed_docs(sqlite_graph_store, "doc_src", "doc_tgt")
    await _insert_valid_edge(sqlite_graph_store, src, tgt, EdgeType.REFERENCES)
    _raw_edge(
        _db(tmp_vault_dir),
        "deadbeef_not_a_uuid",
        src,
        tgt,
        "derived_from",
        notes="kept-note",
        rationale="kept-rationale",
        source_valid_from_version=src,
    )
    before = _all_rows(_db(tmp_vault_dir), "edges")

    rc = run(_db(tmp_vault_dir), apply=False)

    entries, summary = _parse_audit(capsys)
    assert rc == 0
    assert len(entries) == 1
    entry = entries[0]
    assert entry["table"] == "edges"
    assert entry["old_id"] == "deadbeef_not_a_uuid"
    assert entry["new_id"] is None
    assert entry["repairable"] is True
    assert entry["referrer_count"] == 0
    assert entry["source_id"] == src
    assert entry["target_id"] == tgt
    assert entry["edge_type"] == "derived_from"
    assert entry["created_at"] == _NOW.isoformat()
    assert summary["totals"]["edges"]["malformed"] == 1
    assert summary["totals"]["edges"]["repaired"] == 0
    assert _all_rows(_db(tmp_vault_dir), "edges") == before


# ---------------------------------------------------------------------------
# T3
# ---------------------------------------------------------------------------


async def test_apply_reminted_row_preserves_every_other_column(
    sqlite_graph_store, tmp_vault_dir, capsys
):
    """Apply re-mints the id in place; every other column survives.

    The fixture populates every nullable column so the comparison
    covers the full row width.

    Trap: whole-dict equality (not just created_at) catches a
    delete+recreate implementation that regenerates timestamps or
    defaults any column; the rowcount check catches insert-without-
    delete.
    """
    src, tgt = await _seed_docs(sqlite_graph_store, "doc_src", "doc_tgt")
    _raw_edge(
        _db(tmp_vault_dir),
        "deadbeef_full_row",
        src,
        tgt,
        "derived_from",
        resolution_policy="none",
        source_valid_from_version=src,
        target_valid_from_version=tgt,
        valid_until_version=tgt,
        retracted_edge_id=str(uuid.uuid4()),
        created_at="2024-01-02T03:04:05+00:00",
        notes="kept-note",
        rationale="kept-rationale",
        rationale_kind="manual",
        synced_from_version=src,
        synced_from_content_hash=_sha("synced"),
    )
    before = _all_rows(_db(tmp_vault_dir), "edges")
    pre_image = before["deadbeef_full_row"]

    rc = run(_db(tmp_vault_dir), apply=True)

    entries, summary = _parse_audit(capsys)
    assert rc == 0
    assert len(entries) == 1
    new_id = entries[0]["new_id"]
    assert new_id == str(uuid.UUID(new_id))

    after = _all_rows(_db(tmp_vault_dir), "edges")
    assert "deadbeef_full_row" not in after
    assert len(after) == len(before)
    reminted = after[new_id]
    assert reminted == {**pre_image, "id": new_id}
    assert summary["totals"]["edges"]["repaired"] == 1

    strict = await sqlite_graph_store.get_edges_by_source(src)
    assert [e.id for e in strict] == [new_id]


# ---------------------------------------------------------------------------
# T4
# ---------------------------------------------------------------------------


async def test_apply_cascades_retracted_edge_id_referrers_precisely(
    sqlite_graph_store, tmp_vault_dir, capsys
):
    """The cascade updates exactly the referrers of the re-minted id.

    Trap: the untouched second retracts row catches an over-broad
    cascade UPDATE; the query_edges retraction-join assertion catches a
    cascade that wrote the wrong value (a count alone would coincide).
    """
    src, tgt = await _seed_docs(sqlite_graph_store, "doc_src", "doc_tgt")
    v2 = await _insert_valid_edge(sqlite_graph_store, src, tgt, EdgeType.REFERENCES)
    _raw_edge(_db(tmp_vault_dir), "deadbeef_retractee", src, tgt, "supersedes")
    r1 = str(uuid.uuid4())
    _raw_edge(
        _db(tmp_vault_dir),
        r1,
        src,
        None,
        "retracts",
        retracted_edge_id="deadbeef_retractee",
    )
    r2 = str(uuid.uuid4())
    _raw_edge(_db(tmp_vault_dir), r2, src, None, "retracts", retracted_edge_id=v2)

    rc = run(_db(tmp_vault_dir), apply=True)

    entries, summary = _parse_audit(capsys)
    assert rc == 0
    assert len(entries) == 1
    assert entries[0]["referrer_count"] == 1
    assert summary["referrers_updated"] == 1
    new_id = entries[0]["new_id"]

    after = _all_rows(_db(tmp_vault_dir), "edges")
    assert after[r1]["retracted_edge_id"] == new_id
    assert after[r2]["retracted_edge_id"] == v2

    rows, _total = await sqlite_graph_store.query_edges()
    reminted = [r for r in rows if r.edge.id == new_id]
    assert len(reminted) == 1
    assert reminted[0].retracted_by_edge_id == r1


# ---------------------------------------------------------------------------
# T5
# ---------------------------------------------------------------------------


async def test_skips_and_reports_unrepairable_row_without_aborting(
    sqlite_graph_store, tmp_vault_dir, capsys
):
    """A row that stays invalid under a fresh id is reported, not mutated.

    Trap: the co-resident repairable row proves per-row containment —
    an implementation that aborts on the first probe failure leaves it
    unrepaired.
    """
    src, tgt = await _seed_docs(sqlite_graph_store, "doc_src", "doc_tgt")
    _raw_edge(_db(tmp_vault_dir), "deadbeef_bad_type", src, tgt, "not_a_type")
    _raw_edge(_db(tmp_vault_dir), "deadbeef_repairable", src, tgt, "references")

    rc = run(_db(tmp_vault_dir), apply=True)

    entries, summary = _parse_audit(capsys)
    assert rc == 1
    by_old = {e["old_id"]: e for e in entries}
    bad = by_old["deadbeef_bad_type"]
    assert bad["repairable"] is False
    assert bad["new_id"] is None
    assert bad["error"]
    good = by_old["deadbeef_repairable"]
    assert good["repairable"] is True
    assert good["new_id"] == str(uuid.UUID(good["new_id"]))
    assert summary["totals"]["edges"]["repaired"] == 1
    assert summary["totals"]["edges"]["unrepairable"] == 1

    after = _all_rows(_db(tmp_vault_dir), "edges")
    assert "deadbeef_bad_type" in after
    assert after["deadbeef_bad_type"]["edge_type"] == "not_a_type"
    assert "deadbeef_repairable" not in after


# ---------------------------------------------------------------------------
# T6
# ---------------------------------------------------------------------------


async def test_catches_edge_whose_source_id_dangles(sqlite_graph_store, tmp_vault_dir, capsys):
    """A malformed row invisible to per-document enumeration is still found.

    Trap: the positive control (source provably absent from documents)
    catches any scan built on iterating documents and collecting their
    outbound edges — such a scan can never see this row.
    """
    (tgt,) = await _seed_docs(sqlite_graph_store, "doc_tgt")
    ghost = _id("ghost_doc")
    _raw_edge(_db(tmp_vault_dir), "deadbeef_dangling", ghost, tgt, "references")

    conn = sqlite3.connect(_db(tmp_vault_dir))
    try:
        hit = conn.execute("SELECT 1 FROM documents WHERE id = ?", (ghost,)).fetchone()
    finally:
        conn.close()
    assert hit is None

    rc = run(_db(tmp_vault_dir), apply=False)

    entries, _summary = _parse_audit(capsys)
    assert rc == 0
    assert [e["old_id"] for e in entries] == ["deadbeef_dangling"]
    assert entries[0]["source_id"] == ghost


# ---------------------------------------------------------------------------
# T7
# ---------------------------------------------------------------------------


async def test_collision_on_new_id_is_retried_not_destructive(
    sqlite_graph_store, tmp_vault_dir, capsys, monkeypatch
):
    """A minted id colliding with an existing PK is retried, not applied.

    Trap: the victim row's full-column integrity and the stable
    rowcount catch a half-applied UPDATE that survives the
    IntegrityError, and a retry that re-runs the cascade twice.
    """
    import scripts.repair_edge_ids as repair_module

    src, tgt = await _seed_docs(sqlite_graph_store, "doc_src", "doc_tgt")
    victim = await _insert_valid_edge(sqlite_graph_store, src, tgt, EdgeType.REFERENCES)
    _raw_edge(_db(tmp_vault_dir), "deadbeef_collider", src, tgt, "derived_from")
    before = _all_rows(_db(tmp_vault_dir), "edges")

    fresh = str(uuid.uuid4())
    minted = iter([victim, fresh])
    monkeypatch.setattr(repair_module, "_new_edge_id", lambda: next(minted))

    rc = run(_db(tmp_vault_dir), apply=True)

    entries, _summary = _parse_audit(capsys)
    assert rc == 0
    assert len(entries) == 1
    assert entries[0]["repairable"] is True
    assert entries[0]["new_id"] == fresh

    after = _all_rows(_db(tmp_vault_dir), "edges")
    assert len(after) == len(before)
    assert after[victim] == before[victim]
    assert after[fresh] == {**before["deadbeef_collider"], "id": fresh}


# ---------------------------------------------------------------------------
# T8
# ---------------------------------------------------------------------------


async def test_apply_is_idempotent(sqlite_graph_store, tmp_vault_dir, capsys):
    """A second apply run finds nothing and changes nothing.

    Trap: id-set equality between runs catches an implementation that
    re-mints every row each run — it would also report zero malformed
    the second time, but for the wrong reason.
    """
    src, tgt = await _seed_docs(sqlite_graph_store, "doc_src", "doc_tgt")
    _raw_edge(_db(tmp_vault_dir), "deadbeef_once", src, tgt, "references")

    assert run(_db(tmp_vault_dir), apply=True) == 0
    _entries, first_summary = _parse_audit(capsys)
    assert first_summary["totals"]["edges"]["repaired"] == 1
    ids_after_first = set(_all_rows(_db(tmp_vault_dir), "edges"))

    rc = run(_db(tmp_vault_dir), apply=True)

    entries, summary = _parse_audit(capsys)
    assert rc == 0
    assert entries == []
    assert summary["totals"]["edges"]["malformed"] == 0
    assert summary["totals"]["edges"]["scanned"] == first_summary["totals"]["edges"]["scanned"]
    assert set(_all_rows(_db(tmp_vault_dir), "edges")) == ids_after_first


# ---------------------------------------------------------------------------
# T9
# ---------------------------------------------------------------------------


async def test_predicate_excludes_parseable_noncanonical_ids(
    sqlite_graph_store, tmp_vault_dir, capsys
):
    """An id that parses as a UUID — however formatted — is out of scope.

    Strict reads already accept such rows (the validator normalizes),
    so rewriting them would churn ids the contract tolerates.

    Trap: pins the predicate to uuid.UUID parseability; a stricter
    canonical-form regex scan reports and rewrites this row.
    """
    src, tgt = await _seed_docs(sqlite_graph_store, "doc_src", "doc_tgt")
    noncanonical = "ABCDEF01-2345-6789-ABCD-EF0123456789"
    _raw_edge(_db(tmp_vault_dir), noncanonical, src, tgt, "references")
    before = _all_rows(_db(tmp_vault_dir), "edges")

    assert run(_db(tmp_vault_dir), apply=False) == 0
    entries, _summary = _parse_audit(capsys)
    assert entries == []

    assert run(_db(tmp_vault_dir), apply=True) == 0
    entries, _summary = _parse_audit(capsys)
    assert entries == []
    assert _all_rows(_db(tmp_vault_dir), "edges") == before


# ---------------------------------------------------------------------------
# T10
# ---------------------------------------------------------------------------


async def test_handles_retracts_row_that_is_itself_malformed(
    sqlite_graph_store, tmp_vault_dir, capsys
):
    """Both a malformed edge and its malformed retracts referrer repair.

    The retracts row is inserted first so any single-pass scan probes it
    while its retracted_edge_id still carries the malformed target id.

    Trap: catches a classification keyed on stale pre-scan state — the
    referrer is only repairable after the edge it disclaims has been
    re-minted, regardless of processing order.
    """
    src, tgt = await _seed_docs(sqlite_graph_store, "doc_src", "doc_tgt")
    _raw_edge(
        _db(tmp_vault_dir),
        "deadbeef_referrer",
        src,
        None,
        "retracts",
        retracted_edge_id="deadbeef_target",
    )
    _raw_edge(_db(tmp_vault_dir), "deadbeef_target", src, tgt, "supersedes")

    rc = run(_db(tmp_vault_dir), apply=True)

    entries, summary = _parse_audit(capsys)
    assert rc == 0
    assert summary["totals"]["edges"]["repaired"] == 2
    assert summary["totals"]["edges"]["unrepairable"] == 0
    by_old = {e["old_id"]: e for e in entries}
    target_new = by_old["deadbeef_target"]["new_id"]
    referrer_new = by_old["deadbeef_referrer"]["new_id"]

    after = _all_rows(_db(tmp_vault_dir), "edges")
    assert after[referrer_new]["retracted_edge_id"] == target_new


# ---------------------------------------------------------------------------
# T11
# ---------------------------------------------------------------------------


async def test_repairs_malformed_staging_edge_id(sqlite_graph_store, tmp_vault_dir, capsys):
    """Staging-edge ids carry the same contract and repair the same way.

    Trap: the staging table has no retracted_edge_id column — a cascade
    blindly executed against it raises; one keyed against edges for a
    staging repair corrupts. referrer_count must be 0.
    """
    src, tgt = await _seed_docs(sqlite_graph_store, "doc_src", "doc_tgt")
    _raw_staging(_db(tmp_vault_dir), "deadbeef_staging", src, tgt)

    rc = run(_db(tmp_vault_dir), apply=False)
    entries, _summary = _parse_audit(capsys)
    assert rc == 0
    assert [e["table"] for e in entries] == ["staging_edges"]
    assert entries[0]["old_id"] == "deadbeef_staging"

    rc = run(_db(tmp_vault_dir), apply=True)
    entries, summary = _parse_audit(capsys)
    assert rc == 0
    new_id = entries[0]["new_id"]
    assert new_id == str(uuid.UUID(new_id))
    assert entries[0]["referrer_count"] == 0
    assert summary["totals"]["staging_edges"]["repaired"] == 1

    after = _all_rows(_db(tmp_vault_dir), "staging_edges")
    assert "deadbeef_staging" not in after
    assert new_id in after


# ---------------------------------------------------------------------------
# T12
# ---------------------------------------------------------------------------


async def test_repaired_vault_passes_migration_source_read(
    sqlite_graph_store, tmp_vault_dir, capsys
):
    """After repair, the migration tool's source read reports no failures.

    Trap: the pre-repair positive control proves the read CAN fail on
    this store; without it an enumeration that never validates would
    pass vacuously.
    """
    src, tgt = await _seed_docs(sqlite_graph_store, "doc_src", "doc_tgt")
    valid = await _insert_valid_edge(sqlite_graph_store, src, tgt, EdgeType.REFERENCES)
    _raw_edge(_db(tmp_vault_dir), "deadbeef_blocker", src, tgt, "derived_from")

    docs = await sqlite_graph_store.list_all_documents()
    edges_pre, failures_pre = await _collect_edges(sqlite_graph_store, docs)
    assert [f.raw_id for f in failures_pre] == ["deadbeef_blocker"]
    assert [e.id for e in edges_pre] == [valid]

    assert run(_db(tmp_vault_dir), apply=True) == 0
    _parse_audit(capsys)

    edges_post, failures_post = await _collect_edges(sqlite_graph_store, docs)
    assert failures_post == []
    assert len(edges_post) == 2

    rows, total = await sqlite_graph_store.query_edges()
    assert total == 2
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# T13
# ---------------------------------------------------------------------------


async def test_dry_run_is_the_default(sqlite_graph_store, tmp_vault_dir, capsys):
    """Both run() and the CLI mutate nothing unless apply is explicit.

    Trap: catches a signature or argparse wiring where mutation is the
    default — the safe default is the property under test.
    """
    src, tgt = await _seed_docs(sqlite_graph_store, "doc_src", "doc_tgt")
    _raw_edge(_db(tmp_vault_dir), "deadbeef_default", src, tgt, "references")
    before = _all_rows(_db(tmp_vault_dir), "edges")

    rc = run(_db(tmp_vault_dir))
    assert rc == 0
    assert _all_rows(_db(tmp_vault_dir), "edges") == before
    _entries, summary = _parse_audit(capsys)
    assert summary["apply"] is False

    rc = main(["--db", str(_db(tmp_vault_dir))])
    assert rc == 0
    assert _all_rows(_db(tmp_vault_dir), "edges") == before
    _entries, summary = _parse_audit(capsys)
    assert summary["apply"] is False


# ---------------------------------------------------------------------------
# T14
# ---------------------------------------------------------------------------


async def test_audit_line_content_round_trips(sqlite_graph_store, tmp_vault_dir, capsys):
    """Every audit line is parseable JSON with the full field contract.

    Trap: catches entry lines that drift from the documented
    reconstruction contract, and a summary computed from anything other
    than the emitted entries.
    """
    src, tgt = await _seed_docs(sqlite_graph_store, "doc_src", "doc_tgt")
    await _insert_valid_edge(sqlite_graph_store, src, tgt, EdgeType.REFERENCES)
    _raw_edge(_db(tmp_vault_dir), "deadbeef_fixable", src, tgt, "derived_from")
    _raw_edge(_db(tmp_vault_dir), "deadbeef_stuck", src, tgt, "not_a_type")
    _raw_staging(_db(tmp_vault_dir), "deadbeef_stage", src, tgt)

    rc = run(_db(tmp_vault_dir), apply=True)
    assert rc == 1

    entries, summary = _parse_audit(capsys)
    expected_keys = {
        "table",
        "old_id",
        "new_id",
        "source_id",
        "target_id",
        "edge_type",
        "created_at",
        "repairable",
        "error",
        "referrer_count",
    }
    assert len(entries) == 3
    for entry in entries:
        assert set(entry) == expected_keys

    edges_entries = [e for e in entries if e["table"] == "edges"]
    staging_entries = [e for e in entries if e["table"] == "staging_edges"]
    assert summary["apply"] is True
    assert summary["totals"]["edges"]["scanned"] == 3
    assert summary["totals"]["edges"]["malformed"] == len(edges_entries) == 2
    assert summary["totals"]["edges"]["repaired"] == sum(
        1 for e in edges_entries if e["repairable"]
    )
    assert summary["totals"]["edges"]["unrepairable"] == sum(
        1 for e in edges_entries if not e["repairable"]
    )
    assert summary["totals"]["staging_edges"]["scanned"] == 1
    assert summary["totals"]["staging_edges"]["malformed"] == len(staging_entries) == 1
    assert summary["totals"]["staging_edges"]["repaired"] == 1
    assert summary["totals"]["staging_edges"]["unrepairable"] == 0
    assert summary["referrers_updated"] == 0
