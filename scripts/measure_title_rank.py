#!/usr/bin/env python3
"""Measure title rank-1 and recall against a real vault's corpus.

CAS-ADR-049 Decision 8 requires a document to be the first result for a query
naming its title, over the renderings a caller might type, *on the retrieval
binding alone* -- independent of any service-layer boost. The decision makes
that a standing bound rather than a one-time check: a change to fusion, to a
ranking weight, or to either normalization transform is verified against it. So
the instrument is committed rather than improvised per change.

Both arms run in one pass over one seeded copy of the corpus, so the only
difference between them is the field under test:

    before  every passage underived, which the generated column's ``coalesce``
            resolves to the passage's address -- bit-exact pre-decision indexing
            rather than an approximation of it
    after   every passage carrying the structure relative to its document

The live vault is read and never written: rows are copied into a disposable
schema this script provisions and drops. Running it does not migrate anything.

**Seed the whole corpus; query only the active titles.** A vault's inactive
documents stay in the index and compete for every query, so a harness that
seeded only what it queries measures an uncrowded corpus and reports an inflated
rank-1. The report names both counts so the two cannot be confused later.

**Which side of the binding a run can see.** The document surface is copied out
of the vault as stored, so a run measures the query side against whatever index
text the corpus was ingested under. That is the right default -- it is the state
a deployed vault is actually in -- but it leaves the instrument blind to a change
in the *index-side* transform: the stored text predates the change, so the run
reports a tie whatever the change did, and a tie reads as "no effect" rather
than "not measured". ``--recompose-surfaces`` closes that by composing each
document's surface from its own record through the production composition, which
is the state a re-ingested corpus would be in. The report names which of the two
it ran in, because figures from one are not comparable to figures from the other.

Usage::

    .venv/bin/python -m scripts.measure_title_rank --vault cas
    .venv/bin/python -m scripts.measure_title_rank --vault cas --json out.json
    .venv/bin/python -m scripts.measure_title_rank --vault cas --recompose-surfaces
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sage.adapters.content_store_postgres import PostgresContentStore  # noqa: E402
from sage.adapters.interfaces import Chunk, DocumentSurface  # noqa: E402
from sage.models.schemas import Document  # noqa: E402
from sage.services.document_surface import compose_document_surface  # noqa: E402
from sage.services.passage_structure import indexed_structure  # noqa: E402
from sage.storage.postgres.schema import (  # noqa: E402
    assert_disposable_target,
    bootstrap_schema,
)
from sage.utils.text_normalization import fold_for_query  # noqa: E402

# How deep a document may rank and still count toward recall. Rank-1 is the
# decision's actual bound; recall is reported beside it because a document that
# stopped being reachable at all is a different failure from one that slipped.
_RECALL_DEPTH = 20

# The `documents` columns a run reads. Wider than the three the sweep itself
# needs, because recomposing a surface hands the composition a real record
# rather than a stand-in with placeholders in the fields it does not happen to
# read today. Every field the composition reads must be here, which
# ``test_the_projection_covers_every_field_the_composition_reads`` asserts
# against the composition rather than against a second list of names.
_DOCUMENT_COLUMNS = (
    "id",
    "title",
    "source_type",
    "source_path",
    "lifecycle_status",
    "version_label",
    "project",
    "tags",
    "authority_scope",
    "doc_type",
    "source_content_hash",
    "adapter_version",
    "created_by",
    "created_at",
    "last_modified_by",
    "updated_at",
    "semantic_abstract",
)

# How the report names the state its figures were measured in. Spelled out
# rather than left to the flag's absence, because the two are not comparable
# and a reader arriving at a saved report has no other way to tell them apart.
_SURFACE_MODE = {
    False: "as the vault stores them (index-side transform changes are invisible)",
    True: "recomposed from each document record (as a re-ingested corpus would hold them)",
}


# Schemas this script provisions. The `finally` below drops the one it made, but
# a run that takes minutes over a large corpus is realistically ended by killing
# it, and nothing else reclaims what that leaves: the test harness's orphan
# sweep targets `sage_test_db_*` *databases*, not schemas. So each run clears
# what earlier ones stranded, which also keeps a full copy of a large corpus --
# embeddings and vector index included -- from accumulating inside the working
# database.
_SCHEMA_PREFIX = "sage_test_titlerank_"


async def _sweep_orphaned_schemas(conn) -> None:
    """Drop schemas a killed earlier run left behind."""
    cur = await conn.execute(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE %s",
        (_SCHEMA_PREFIX + "%",),
    )
    for (stale,) in await cur.fetchall():
        await conn.execute(f'DROP SCHEMA IF EXISTS "{assert_disposable_target(stale)}" CASCADE')  # noqa: S608


@dataclass
class ArmResult:
    """One arm's sweep over one rendering of every queried title."""

    rendering: str
    rank_1: int = 0
    recalled: int = 0
    total: int = 0
    ranks: dict[str, int | None] = field(default_factory=dict)

    @property
    def rank_1_rate(self) -> float:
        return self.rank_1 / self.total if self.total else 0.0

    @property
    def recall_rate(self) -> float:
        return self.recalled / self.total if self.total else 0.0


def _renderings(title: str) -> dict[str, str]:
    """The renderings CAS-ADR-049 Decision 8 holds over.

    "However a caller types the separators, the case, and the word boundaries
    of the title" -- so the sweep is over forms, not over one string.
    """
    forms = {
        "verbatim": title,
        "lowercase": title.lower(),
        "uppercase": title.upper(),
        "separators folded": fold_for_query(title),
    }
    # Join runs of plain words only. Identifier punctuation, numeric tokens,
    # existing compounds and non-ASCII words are not rewritten. Ineligible
    # titles contribute no compound observation, rather than a duplicate.
    compound = re.sub(
        r"(?<!\S)(?:[A-Z]+|[A-Z]?[a-z]+)(?:[ \t]+(?:[A-Z]+|[A-Z]?[a-z]+))+(?!\S)",
        lambda match: (
            match.group().split()[0].lower()
            + "".join(word.capitalize() for word in match.group().split()[1:])
        ),
        title,
    )
    if compound != title:
        forms["compound"] = compound
    return forms


async def _read_corpus(dsn: str, vault: str) -> tuple[list, list, list]:
    """Copy a vault's passages, surfaces and documents out. Reads only."""
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(f'SET search_path TO "{vault}", public')  # noqa: S608
        chunks = await (
            await conn.execute(
                "SELECT document_id, heading_path, content, chunk_index, embedding, "
                "doc_type, lifecycle_status, project FROM chunks"
            )
        ).fetchall()
        surfaces = await (
            await conn.execute(
                "SELECT document_id, matchable, orienting, embedding, doc_type, "
                "lifecycle_status, project FROM document_surface"
            )
        ).fetchall()
        # Interpolated, but every name comes from a fixed tuple in this module
        # and nothing caller-supplied reaches the text.
        columns = ", ".join(_DOCUMENT_COLUMNS)
        cursor = conn.cursor(row_factory=dict_row)
        documents = await (
            await cursor.execute(f"SELECT {columns} FROM documents")  # noqa: S608
        ).fetchall()
    return chunks, surfaces, documents


def _as_embedding(value):
    """Pass a vector through unchanged.

    Read back over a connection with no pgvector type registered, the column
    arrives as its text literal (``[0.1,...]``), which Postgres casts back on
    the way in. Only a real sequence needs materializing -- calling ``list`` on
    the literal would shred it into characters.
    """
    if value is None or isinstance(value, str):
        return value
    return list(value)


def _surface_row(copied, document: Document | None, *, recompose: bool) -> DocumentSurface:
    """The surface to seed: the vault's own text, or text recomposed now.

    ``copied`` is one row as the vault stores it. With ``recompose`` off it is
    seeded verbatim, which is what puts the run in the state a deployed vault is
    actually in. With it on, the text halves are rebuilt from the document's own
    record through the production composition -- the state the same vault would
    be in once re-ingested -- and the index-side transform becomes something the
    run can see.

    The vector is carried across from the copied row either way. Recomposing it
    would mean re-embedding the corpus, which is a different and far more
    expensive claim than the one this instrument makes, and the keyword arm the
    figures come from does not read it.
    """
    document_id, matchable, orienting, embedding, doc_type, lifecycle_status, project = copied
    if recompose and document is not None:
        composed = compose_document_surface(document_id, document)
        matchable, orienting = composed.matchable, composed.orienting
    return DocumentSurface(
        document_id=document_id,
        matchable=matchable,
        orienting=orienting,
        embedding=_as_embedding(embedding),
        doc_type=doc_type,
        lifecycle_status=lifecycle_status,
        project=project,
    )


def _document_from_row(row: dict) -> Document:
    """One vault record, rebuilt from the columns a run projects.

    Built the way the store's own row hydrator builds one: ``tags`` coalesced,
    because the column is nullable and the field is not, and construction
    bypassing validation, because the store deliberately tolerates stored
    values its request-side validators would reject. Either omission aborts a
    run before any figure is produced, on a row some vault legitimately holds
    -- and the script is pointed at whatever vault the caller names, so the row
    it cannot hydrate is somebody's corpus rather than a hypothetical.

    Nothing here needs validation to have run: the composition reads the title,
    the tags, the source path and the abstract, and transforms none of them.
    """
    values = {name: row[name] for name in _DOCUMENT_COLUMNS}
    values["tags"] = values["tags"] or []
    return Document.model_construct(**values)


def _recomposition_control(surfaces, records: dict[str, Document] | None) -> tuple[int, int]:
    """How much stored text the recomposition actually rewrote.

    The control on the flag, in the shape ``_arm_control`` already establishes
    for the arms. A recomposed run over a corpus the composition happens to
    reproduce exactly reports a tie that is indistinguishable from a tie it
    measured, and the flag exists precisely because a tie of the first kind
    reads as a result. Reporting the count makes the two tellable apart: zero
    of a thousand rows rewritten means the run had nothing to see.

    Both halves are compared. The recomposition rewrites the derived half too,
    and that half carries the source-filename stem's expansion -- which is
    where a compound identifier most often lives, and which ranks. A count
    reading the authored half alone reports a row rewritten only in its
    derived text as untouched, and so reports "nothing to see" about a run that
    moved a ranking input.
    """
    if records is None:
        return 0, len(surfaces)
    changed = 0
    for copied in surfaces:
        record = records.get(copied[0])
        if record is None:
            continue
        composed = compose_document_surface(copied[0], record)
        if (composed.matchable, composed.orienting) != (copied[1], copied[2]):
            changed += 1
    return changed, len(surfaces)


async def _load(
    store: PostgresContentStore,
    chunks,
    surfaces,
    titles,
    *,
    derived: bool,
    records: dict[str, Document] | None = None,
) -> None:
    """Seed one arm. ``derived`` selects which side of the change is measured.

    ``records`` present means the surfaces are recomposed from them rather than
    seeded as the vault stores them; absent means seeded verbatim.
    """
    by_document: dict[str, list[Chunk]] = {}
    for document_id, address, content, index, embedding, dt, ls, project in chunks:
        by_document.setdefault(document_id, []).append(
            Chunk(
                document_id=document_id,
                heading_path=address,
                indexed_structure=(
                    indexed_structure(address, titles.get(document_id)) if derived else None
                ),
                content=content,
                chunk_index=index,
                embedding=_as_embedding(embedding),
                doc_type=dt,
                lifecycle_status=ls,
                project=project,
            )
        )
    for document_id, document_chunks in by_document.items():
        await store.index_chunks(document_id, document_chunks)

    for copied in surfaces:
        record = records.get(copied[0]) if records is not None else None
        await store.upsert_document_surface(
            _surface_row(copied, record, recompose=records is not None)
        )


async def _arm_control(pool) -> tuple[int, int]:
    """How much indexed text this arm actually changed.

    The control on the whole measurement. Two arms that tie tell you nothing
    unless they genuinely differ at rest -- a load that silently failed to
    derive anything would report a perfect tie and read as "no effect". This
    reports the passages whose *effective* weight-A text -- what the generated
    column actually indexes, coalesce included -- differs from the address.
    Zero in the before arm and non-zero in the after arm is what makes a tie in
    the ranks below a finding rather than an artifact.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FILTER ("
            " WHERE coalesce(indexed_structure, heading_path) IS DISTINCT FROM heading_path"
            "), count(*) FROM chunks"
        )
        changed, total = await cur.fetchone()
    return changed, total


async def _sweep(store: PostgresContentStore, queried: dict[str, str]) -> dict[str, ArmResult]:
    """Rank every queried title, in every rendering, on the binding alone."""
    results: dict[str, ArmResult] = {}
    for document_id, title in queried.items():
        for name, query in _renderings(title).items():
            arm = results.setdefault(name, ArmResult(rendering=name))
            arm.total += 1
            hits = await store.search_bm25(query, limit=_RECALL_DEPTH)
            position = next(
                (i for i, hit in enumerate(hits) if hit.document_id == document_id), None
            )
            arm.ranks[document_id] = position
            if position is not None:
                arm.recalled += 1
                if position == 0:
                    arm.rank_1 += 1
    return results


def _render(  # noqa: PLR0913 -- a report renderer takes what the report shows
    vault,
    seeded,
    queried,
    applicable,
    before,
    after,
    before_control,
    after_control,
    recomposed=False,
    recomposition_control=None,
) -> str:
    lines = [
        f"Title rank-1 against the {vault!r} corpus, on the retrieval binding alone.",
        "",
        f"  corpus seeded      {seeded} documents (all of them compete for every query)",
        f"  titles queried     {queried} active documents",
        f"  surfaces           {_SURFACE_MODE[recomposed]}",
        *(
            [
                f"  surface control    {recomposition_control[0]}/{recomposition_control[1]} "
                f"rows carry text differing from what the vault stores"
            ]
            if recomposed and recomposition_control is not None
            else []
        ),
        f"  applicable         {applicable:.1%} of passages are rooted at their document's title",
        f"  control            {before_control[0]}/{before_control[1]} passages carry text "
        f"differing from their address before, {after_control[0]}/{after_control[1]} after",
        "",
        f"{'rendering':<20} {'eligible':>8} {'rank-1 before':>14} {'rank-1 after':>13} "
        f"{'recall before':>14} {'recall after':>13}",
    ]
    for name in before:
        b, a = before[name], after[name]
        lines.append(
            f"{name:<20} {b.total:>8} {b.rank_1_rate:>13.1%} {a.rank_1_rate:>12.1%} "
            f"{b.recall_rate:>13.1%} {a.recall_rate:>12.1%}"
        )

    moved = []
    for name in before:
        for document_id, was in before[name].ranks.items():
            now = after[name].ranks.get(document_id)
            if (was == 0) != (now == 0):
                moved.append((name, document_id, was, now))
    if moved:
        lines += ["", "Documents whose rank-1 status changed:"]
        lines += [
            f"  [{name}] {document_id}: {was} -> {now}" for name, document_id, was, now in moved
        ]
    else:
        lines += ["", "No document changed rank-1 status in any rendering."]
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, help="Vault (schema) to read the corpus from.")
    parser.add_argument("--dsn", default=os.environ.get("SAGE_MEASURE_DSN"))
    parser.add_argument("--json", type=Path, help="Also write the figures here.")
    parser.add_argument(
        "--recompose-surfaces",
        action="store_true",
        help=(
            "Rebuild each document surface from its own record through the "
            "production composition, instead of seeding the text the vault "
            "stores. Needed to see a change to the index-side transform, which "
            "stored text predates; figures are not comparable to a run without it."
        ),
    )
    args = parser.parse_args()

    dsn = args.dsn or "postgresql://localhost:5432/sage"
    chunks, surfaces, documents = await _read_corpus(dsn, args.vault)
    if not chunks:
        print(f"vault {args.vault!r} holds no passages", file=sys.stderr)
        return 1

    titles = {row["id"]: row["title"] for row in documents}
    queried = {
        row["id"]: row["title"]
        for row in documents
        if row["lifecycle_status"] == "active" and row["title"]
    }
    records = (
        {row["id"]: _document_from_row(row) for row in documents}
        if args.recompose_surfaces
        else None
    )
    rooted = sum(
        1
        for document_id, address, *_ in chunks
        if indexed_structure(address, titles.get(document_id)) != address
    )
    applicable = rooted / len(chunks)

    schema = assert_disposable_target(_SCHEMA_PREFIX + os.urandom(4).hex())
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await _sweep_orphaned_schemas(conn)
        await bootstrap_schema(conn, schema=schema, extensions=["vector"])

    pool = AsyncConnectionPool(
        dsn, min_size=1, max_size=4, open=False, configure=_search_path_setter(schema)
    )
    await pool.open()
    try:
        store = PostgresContentStore(pool)

        await _load(store, chunks, surfaces, titles, derived=False, records=records)
        before_control = await _arm_control(pool)
        before = await _sweep(store, queried)

        await _load(store, chunks, surfaces, titles, derived=True, records=records)
        after_control = await _arm_control(pool)
        after = await _sweep(store, queried)
    finally:
        await pool.close()
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')  # noqa: S608

    report = _render(
        args.vault,
        len(documents),
        len(queried),
        applicable,
        before,
        after,
        before_control,
        after_control,
        recomposed=records is not None,
        recomposition_control=_recomposition_control(surfaces, records),
    )
    print(report)
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "vault": args.vault,
                    "seeded_documents": len(documents),
                    "queried_documents": len(queried),
                    "applicable_passage_share": applicable,
                    "surfaces_recomposed": records is not None,
                    "before": {
                        name: {
                            "rank_1": arm.rank_1_rate,
                            "recall": arm.recall_rate,
                            "total": arm.total,
                            "ranks": arm.ranks,
                        }
                        for name, arm in before.items()
                    },
                    "after": {
                        name: {
                            "rank_1": arm.rank_1_rate,
                            "recall": arm.recall_rate,
                            "total": arm.total,
                            "ranks": arm.ranks,
                        }
                        for name, arm in after.items()
                    },
                },
                indent=2,
            )
        )
    return 0


def _search_path_setter(schema: str):
    async def _configure(conn) -> None:
        await conn.execute(f'SET search_path TO "{schema}", public')  # noqa: S608
        await conn.commit()

    return _configure


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
