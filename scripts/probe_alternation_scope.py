#!/usr/bin/env python3
"""Measure what document-scoping a top-level alternation changes, on a real corpus.

CAS-ADR-048 scopes a keyword match to the document. A top-level alternation was
not reaching that scope: the binding declined to decompose the query and
evaluated it whole against a single unit of text instead. Distributing it --
one intersection per branch, unioned -- brings it into scope, which is a recall
change rather than a refactor, so it is measured rather than asserted.

Both arms run in one pass over one seeded copy of the corpus, so the only
difference between them is the code path under test:

    before  ``_search_bm25_within_chunk``, the path an alternation used to
            reach: the whole query, rendered in SQL and never decomposed,
            satisfied within one passage or the document surface
    after   ``search_bm25``, which now decomposes it into branches and resolves
            each branch's operands across the document

Every query is an alternation by construction: a token absent from the corpus
is one branch, so it contributes nothing and the other branch decides alone.
Two families supply that other branch, because they answer different questions
and only together cover the change:

    title           the document's own title, in the renderings
                    ``scripts.measure_title_rank`` sweeps. Asks whether adding
                    a disjunct to a query costs precision. It does not reach
                    the widening: a title is carried whole on the document
                    surface, which is one unit of text, so the before arm
                    already satisfies the branch there (CAS-ADR-049).
    cross-passage   two of the document's own terms, drawn from different
                    passages, with no single passage of that document carrying
                    both. This is the shape the change is *about* -- the before
                    arm can satisfy the branch nowhere, since no unit holds
                    both terms, and the after arm assembles it across the
                    document.

The live vault is read and never written: rows are copied into a disposable
schema this script provisions and drops. Running it does not migrate anything.

**Seed the whole corpus; query only the active titles.** A vault's inactive
documents stay in the index and compete for every query, so a harness that
seeded only what it queries measures an uncrowded corpus and reports an inflated
rank-1. The report names both counts so the two cannot be confused later.

Usage::

    .venv/bin/python -m scripts.probe_alternation_scope --vault cas
    .venv/bin/python -m scripts.probe_alternation_scope --vault cas --json out.json
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
from psycopg_pool import AsyncConnectionPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sage.adapters.content_store_postgres import (  # noqa: E402
    PostgresContentStore,
    _passage_rows_only,
)
from sage.adapters.interfaces import Chunk, DocumentSurface  # noqa: E402
from sage.services.passage_structure import indexed_structure  # noqa: E402
from sage.storage.postgres.schema import (  # noqa: E402
    TEXT_SEARCH_CONFIG,
    assert_disposable_target,
    bootstrap_schema,
)
from sage.utils.text_normalization import fold_for_query  # noqa: E402

# How deep a document may rank and still count toward recall. Held equal to the
# title instrument's depth so the two reports read against each other.
_RECALL_DEPTH = 20

# The branch that must contribute nothing. A corpus term here would make the
# other branch's contribution unreadable, since either could carry the match.
_ABSENT_BRANCH = "zzqabsentbranchtoken"

# Schemas this script provisions. The `finally` below drops the one it made, but
# a run that takes minutes over a large corpus is realistically ended by killing
# it, and nothing else reclaims what that leaves: the test harness's orphan
# sweep targets `sage_test_db_*` *databases*, not schemas. So each run clears
# what earlier ones stranded, which also keeps a full copy of a large corpus --
# embeddings and vector index included -- from accumulating inside the working
# database.
_SCHEMA_PREFIX = "sage_test_altscope_"


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
    """The renderings the title instrument sweeps, each made an alternation.

    Kept equal to that sweep's forms so a figure here can be read against the
    figure there: what differs between the two reports is then the disjunct and
    nothing else.
    """
    return {
        name: f"{_ABSENT_BRANCH} or {form}"
        for name, form in {
            "verbatim": title,
            "lowercase": title.lower(),
            "uppercase": title.upper(),
            "separators folded": fold_for_query(title),
        }.items()
    }


# A word worth querying on: long enough that the text-search configuration keeps
# it, and alphabetic so no identifier's punctuation turns one term into several.
_WORD = re.compile(r"[a-z]{6,}")


def _passage_words(content: str) -> set[str]:
    return set(_WORD.findall(content.lower()))


def _cross_passage_pairs(chunks, stem) -> dict[str, tuple[str, str]]:
    """One pair of terms per document that no single passage of it carries.

    The pair is what the before arm cannot satisfy anywhere: each term lives in
    the document, and no unit of text holds both, so a conjunction of the two
    matches at document scope and nowhere narrower. Documents offering no such
    pair are dropped rather than given a weaker query, since a pair one passage
    happens to hold would be answered by both arms and dilute the figures
    toward zero.

    Rarity picks the terms. The rarest word in the corpus is the one whose
    presence in the result is most plainly the query's doing rather than the
    ranking's, and taking the rarest from each of two passages makes the choice
    a property of the corpus rather than of whoever wrote the probe.

    ``stem`` maps a word to the lexeme the text-search configuration reduces it
    to, and everything except the returned pair is decided on lexemes. It has
    to be: matching is stemmed, so a passage holding ``documents`` satisfies a
    term ``document``, and a selection reading raw words would call such a pair
    cross-passage and hand the before arm a query it can answer within one unit
    after all.

    The *terms* stay raw words even so, because a lexeme is not a safe query:
    the English stemmer is not idempotent, and rendering an already-stemmed
    word stems it a second time to something the index never wrote.

    What this produces is a *candidate* set, and the docstring says candidate
    because the lexeme sets here are assembled from the words the regex above
    yields -- so a lexeme the index holds through a shorter inflection is
    invisible to them, and a pair can survive that a passage answers.
    ``_keep_unsatisfied_pairs`` settles it against the index itself; nothing
    downstream should treat this function's output as the guarantee.
    """
    by_document: dict[str, list[set[str]]] = {}
    frequency: dict[str, int] = {}
    for document_id, _address, content, *_ in chunks:
        words = _passage_words(content)
        by_document.setdefault(document_id, []).append(words)
        for lexeme in {stem(word) for word in words if stem(word)}:
            frequency[lexeme] = frequency.get(lexeme, 0) + 1

    def _rank(word: str) -> tuple[int, str]:
        return (frequency.get(stem(word), 0), word)

    def _queryable(words):
        # A word the configuration discards renders to nothing, so a pair
        # holding one asks for a single lexeme and is not a conjunction.
        return {word for word in words if stem(word)}

    pairs: dict[str, tuple[str, str]] = {}
    for document_id, passages in by_document.items():
        if len(passages) < 2:
            continue
        # The rarest word of each passage, then the rarest two of those whose
        # lexemes no one passage holds together.
        queryable = [_queryable(words) for words in passages]
        rarest = sorted((min(words, key=_rank) for words in queryable if words), key=_rank)
        lexemes = [{stem(word) for word in words} for words in queryable]
        pair = next(
            (
                (first, second)
                for i, first in enumerate(rarest)
                for second in rarest[i + 1 :]
                # Distinct *as the index sees them*: two spellings of one lexeme
                # are one term, and a conjunction of a term with itself is not a
                # cross-passage query.
                if stem(first) != stem(second)
                and not any({stem(first), stem(second)} <= held for held in lexemes)
            ),
            None,
        )
        if pair:
            pairs[document_id] = pair
    return pairs


async def _stem_map(dsn: str, words: set[str]) -> dict[str, str]:
    """Each word's lexeme under the text-search configuration, in one round-trip.

    Asked of the backend rather than reimplemented, so the selection and the
    match agree on what a word reduces to. A word the configuration discards
    reports itself; such a word is dropped as a candidate by the caller, since
    a pair holding one renders to a single required lexeme and stops being a
    conjunction at all.
    """
    ordered = sorted(words)
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        cur = await conn.execute(
            "SELECT w, (SELECT lexeme FROM unnest("  # noqa: S608
            f"to_tsvector('{TEXT_SEARCH_CONFIG}', w)) LIMIT 1)"
            " FROM unnest(%s::text[]) AS w",
            (ordered,),
        )
        return {word: lexeme for word, lexeme in await cur.fetchall()}


async def _keep_unsatisfied_pairs(
    pool, pairs: dict[str, tuple[str, str]]
) -> tuple[dict[str, tuple[str, str]], int]:
    """Drop any pair some passage of its own document already satisfies.

    The selection above reasons over lexemes it assembles itself, and an
    assembled set is only as complete as the words it was built from: a term's
    lexeme can reach a passage through an inflection the candidate regex never
    yielded, and the pair then looks cross-passage while a single passage
    answers it. Two of 199 survived the previous guard that way, which was the
    whole of what the before arm appeared to reach.

    So the last word goes to the predicate the before arm actually evaluates,
    over the seeded rows it will actually read -- same tokenizer, same operator,
    same text. Nothing is derived here, which is the point: a guard that reasons
    can be incomplete in a way that a guard that asks cannot.

    One statement rather than one per document. A document whose pair is
    satisfied is dropped rather than given the next candidate: silence beats a
    weaker query, and the alternative reintroduces the selection this exists to
    check.
    """
    if not pairs:
        return {}, 0
    rows = [(document_id, f"{first} {second}") for document_id, (first, second) in pairs.items()]
    values = ", ".join(["(%s, %s)"] * len(rows))
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT v.document_id FROM (VALUES {values}) AS v(document_id, q)"  # noqa: S608
            " WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = v.document_id"
            f" AND {_passage_rows_only('c')}"
            f" AND c.tsv @@ websearch_to_tsquery('{TEXT_SEARCH_CONFIG}', v.q))",
            [value for row in rows for value in row],
        )
        satisfied = {document_id for (document_id,) in await cur.fetchall()}
    return {
        document_id: pair for document_id, pair in pairs.items() if document_id not in satisfied
    }, len(satisfied)


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
        documents = await (
            await conn.execute("SELECT id, title, lifecycle_status FROM documents")
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


async def _load(store: PostgresContentStore, chunks, surfaces, titles) -> None:
    """Seed the corpus once. Both arms read it; neither writes.

    The arms differ by code path, not by indexed data, so unlike the title
    instrument this seeds a single time and the ``derived`` toggle has no
    counterpart here. Passages carry their structure relative to the document,
    which is what the vault itself holds.
    """
    by_document: dict[str, list[Chunk]] = {}
    for document_id, address, content, index, embedding, dt, ls, project in chunks:
        by_document.setdefault(document_id, []).append(
            Chunk(
                document_id=document_id,
                heading_path=address,
                indexed_structure=indexed_structure(address, titles.get(document_id)),
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

    for document_id, matchable, orienting, embedding, dt, ls, project in surfaces:
        await store.upsert_document_surface(
            DocumentSurface(
                document_id=document_id,
                matchable=matchable,
                orienting=orienting,
                embedding=_as_embedding(embedding),
                doc_type=dt,
                lifecycle_status=ls,
                project=project,
            )
        )


async def _before(store: PostgresContentStore, query: str):
    """The path an alternation reached before it was distributed."""
    return await store._search_bm25_within_chunk(query, _RECALL_DEPTH, None)  # noqa: SLF001


async def _after(store: PostgresContentStore, query: str):
    """The dispatcher, which now routes an alternation to document scope."""
    return await store.search_bm25(query, limit=_RECALL_DEPTH)


def _title_queries(queried: dict[str, str]) -> dict[str, dict[str, str]]:
    """The title family: every rendering of every active title, alternated."""
    return {document_id: _renderings(title) for document_id, title in queried.items()}


def _pair_queries(pairs: dict[str, tuple[str, str]]) -> dict[str, dict[str, str]]:
    """The cross-passage family, with the bare conjunction beside it.

    Both columns matter and neither alone would do. The alternation is the
    shape under measurement; the bare conjunction is the positive control, read
    in the *after* arm: it says the pair is reachable at document scope at all,
    so a low alternation figure would read as the alternation failing rather
    than as the terms having been badly chosen. The two after columns agreeing
    is the finding stated a second way -- adding a disjunct now costs a query
    nothing.
    """
    return {
        document_id: {
            "alternation": f"{_ABSENT_BRANCH} or {first} {second}",
            "bare conjunction": f"{first} {second}",
        }
        for document_id, (first, second) in pairs.items()
    }


async def _arm_control(store: PostgresContentStore, queries: dict[str, dict[str, str]]) -> int:
    """How many queries the two arms actually answer differently.

    The control on the whole measurement. Two arms that tie tell you nothing
    unless they genuinely differ at rest -- an ``_after`` that had silently
    routed back to the fallback would report a perfect tie and read as "no
    effect". Row shape is the published difference between the two paths, so
    this counts the queries whose answers disagree in shape or membership.
    Non-zero is what makes a tie in the rates below a finding rather than an
    artifact.
    """
    differing = 0
    for forms in queries.values():
        query = next(iter(forms.values()))
        before = await _before(store, query)
        after = await _after(store, query)
        if [(h.document_id, h.matched_chunk_count) for h in before] != [
            (h.document_id, h.matched_chunk_count) for h in after
        ]:
            differing += 1
    return differing


async def _sweep(
    store: PostgresContentStore, queries: dict[str, dict[str, str]], arm
) -> dict[str, ArmResult]:
    """Rank every document against its own queries, in every form, through one arm."""
    results: dict[str, ArmResult] = {}
    for document_id, forms in queries.items():
        for name, query in forms.items():
            result = results.setdefault(name, ArmResult(rendering=name))
            result.total += 1
            hits = await arm(store, query)
            position = next(
                (i for i, hit in enumerate(hits) if hit.document_id == document_id), None
            )
            result.ranks[document_id] = position
            if position is not None:
                result.recalled += 1
                if position == 0:
                    result.rank_1 += 1
    return results


def _table(before: dict[str, ArmResult], after: dict[str, ArmResult]) -> list[str]:
    lines = [
        f"{'form':<20} {'rank-1 before':>14} {'rank-1 after':>13} "
        f"{'recall before':>14} {'recall after':>13}",
    ]
    for name in before:
        b, a = before[name], after[name]
        lines.append(
            f"{name:<20} {b.rank_1_rate:>13.1%} {a.rank_1_rate:>12.1%} "
            f"{b.recall_rate:>13.1%} {a.recall_rate:>12.1%}"
        )
    newly = [
        (name, document_id)
        for name in before
        for document_id, was in before[name].ranks.items()
        if (was is None) != (after[name].ranks.get(document_id) is None)
    ]
    lines.append(
        f"  reachability changed for {len(newly)} of "
        f"{sum(arm.total for arm in before.values())} queries"
    )
    return lines


def _render(  # noqa: PLR0913 -- a report renderer takes what the report shows
    vault, seeded, titles_queried, pairs, title_arms, pair_arms, controls
) -> str:
    pairs_queried, leaked = pairs
    return "\n".join(
        [
            f"Alternation scope against the {vault!r} corpus, on the retrieval binding alone.",
            f"Every alternation is {_ABSENT_BRANCH!r} or <branch>: one branch absent by",
            "construction, so the other decides alone.",
            "",
            f"  corpus seeded      {seeded} documents (all of them compete for every query)",
            f"  titles queried     {titles_queried} active documents",
            f"  pairs queried      {pairs_queried} documents offering two terms no one "
            "passage holds together",
            f"  pairs dropped      {leaked} candidates a passage answered after all, "
            "rejected by the index rather than by the selection",
            f"  control            titles {controls[0]}/{titles_queried}, pairs "
            f"{controls[1]}/{pairs_queried} queries answered differently by the two arms",
            "",
            "Title branch -- reachable within one unit already, since a document surface",
            "carries the whole title. Measures what the widening costs, not what it adds.",
            "",
            *_table(*title_arms),
            "",
            "Cross-passage branch -- two of the document's own terms that no single passage",
            "holds together. Measures what the widening adds.",
            "",
            *_table(*pair_arms),
        ]
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, help="Vault (schema) to read the corpus from.")
    parser.add_argument("--dsn", default=os.environ.get("SAGE_MEASURE_DSN"))
    parser.add_argument("--json", type=Path, help="Also write the figures here.")
    args = parser.parse_args()

    dsn = args.dsn or "postgresql://localhost:5432/sage"
    chunks, surfaces, documents = await _read_corpus(dsn, args.vault)
    if not chunks:
        print(f"vault {args.vault!r} holds no passages", file=sys.stderr)
        return 1

    titles = {row[0]: row[1] for row in documents}
    queried = {row[0]: row[1] for row in documents if row[2] == "active" and row[1]}
    # Pairs are drawn from the active slice too, so both families ask about the
    # same documents and a difference between the tables is the query shape.
    # Lexemes, not raw words, decide which pairs qualify -- see
    # ``_cross_passage_pairs``. The vocabulary is asked for once, before the
    # disposable schema exists, since it is a property of the configuration
    # rather than of the seeded copy.
    vocabulary = {word for _, _, content, *_ in chunks for word in _passage_words(content)}
    stem = await _stem_map(dsn, vocabulary)
    candidates = {
        document_id: pair
        for document_id, pair in _cross_passage_pairs(chunks, stem.get).items()
        if document_id in queried
    }
    title_queries = _title_queries(queried)

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
        await _load(store, chunks, surfaces, titles)
        # Settled against the seeded rows, so the guard is the arm's own
        # predicate rather than a reconstruction of it.
        pairs, leaked = await _keep_unsatisfied_pairs(pool, candidates)
        pair_queries = _pair_queries(pairs)
        controls = (
            await _arm_control(store, title_queries),
            await _arm_control(store, pair_queries),
        )
        title_arms = (
            await _sweep(store, title_queries, _before),
            await _sweep(store, title_queries, _after),
        )
        pair_arms = (
            await _sweep(store, pair_queries, _before),
            await _sweep(store, pair_queries, _after),
        )
    finally:
        await pool.close()
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')  # noqa: S608

    report = _render(
        args.vault,
        len(documents),
        len(queried),
        (len(pairs), leaked),
        title_arms,
        pair_arms,
        controls,
    )
    print(report)
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "vault": args.vault,
                    "seeded_documents": len(documents),
                    "titles_queried": len(queried),
                    "pairs_queried": len(pairs),
                    "pairs_dropped_as_satisfied": leaked,
                    "arms_differing": {"title": controls[0], "cross_passage": controls[1]},
                    "title": _as_figures(*title_arms),
                    "cross_passage": _as_figures(*pair_arms),
                },
                indent=2,
            )
        )
    return 0


def _as_figures(before: dict[str, ArmResult], after: dict[str, ArmResult]) -> dict:
    return {
        arm_name: {
            name: {"rank_1": arm.rank_1_rate, "recall": arm.recall_rate}
            for name, arm in results.items()
        }
        for arm_name, results in (("before", before), ("after", after))
    }


def _search_path_setter(schema: str):
    async def _configure(conn) -> None:
        await conn.execute(f'SET search_path TO "{schema}", public')  # noqa: S608
        await conn.commit()

    return _configure


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
