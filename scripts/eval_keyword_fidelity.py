#!/usr/bin/env python3
"""Evaluate keyword-backend fidelity: LanceDB BM25 vs Postgres ``ts_rank``.

Builds a disposable corpus by copying a representative slice of an existing
SAGE vault's chunks (with their stored embeddings) into a throwaway LanceDB
content store, and loads the identical chunk text into a throwaway Postgres
table. For each query in the query set the harness fetches three rankings --
the vector ranking (the constant arm), LanceDB BM25 (keyword arm A), and
Postgres ``ts_rank`` (keyword arm B) -- fuses each keyword arm with the shared
vector arm through the production Reciprocal Rank Fusion, and compares the fused
top-K. Output is a scorecard plus a go/no-go recommendation (default to native
``ts_rank``, or escalate to a managed search service).

The eval never queries the source vault: it copies a slice out and runs entirely
against the throwaway store and table, which it owns end-to-end and tears down on
exit. It reads the source vault only through SAGE's own content-store adapter and
makes no change to it.

Requires the ``eval`` extra (``uv sync --extra eval``) for ``psycopg`` and a
reachable Postgres named by ``--pg-dsn`` / ``SAGE_EVAL_PG_DSN``. Stand up a
throwaway instance, for example::

    initdb -D /tmp/pgeval && pg_ctl -D /tmp/pgeval -o "-p 5599" -l /tmp/pgeval.log start
    createdb -p 5599 kweval
    export SAGE_EVAL_PG_DSN="postgresql://localhost:5599/kweval"

    .venv/bin/python -m scripts.eval_keyword_fidelity --source-vault cas \\
        --corpus-size 40 --k 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from sage.adapters.content_store_lancedb import LanceDBContentStore
from sage.adapters.stubs import StubAbstractionProvider
from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.utils.keyword_fidelity_eval import (
    PostgresFTSBackend,
    render_scorecard,
    run_fidelity_eval,
)
from sage.vault_management import config_path_for_vault

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUERIES = REPO_ROOT / "tests" / "fixtures" / "keyword_eval_queries.yaml"
DEFAULT_OUTPUT_DIR = Path.home() / "sage_eval_output"


def _load_queries(path: Path) -> list[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [str(q) for q in data["queries"]]


def _select_slice(candidates: list[tuple[str, str]], size: int, seed: int) -> list[str]:
    """Stratify ``(doc_id, doc_type)`` by doc_type and round-robin up to ``size``.

    Round-robin across doc_types gives a slice that spans the corpus's document
    kinds rather than over-sampling whichever type is most numerous.
    """
    by_type: dict[str, list[str]] = {}
    for doc_id, doc_type in candidates:
        by_type.setdefault(doc_type, []).append(doc_id)
    rng = random.Random(seed)  # noqa: S311 -- corpus sampling, not cryptographic
    for ids in by_type.values():
        rng.shuffle(ids)

    types = sorted(by_type)
    cursors = {t: 0 for t in types}
    selected: list[str] = []
    while len(selected) < size:
        progressed = False
        for t in types:
            if cursors[t] < len(by_type[t]):
                selected.append(by_type[t][cursors[t]])
                cursors[t] += 1
                progressed = True
                if len(selected) >= size:
                    break
        if not progressed:
            break
    return selected


def _serialize(result, corpus_docs: int, corpus_chunks: int) -> dict:  # noqa: ANN001
    payload = asdict(result)
    payload["corpus_docs"] = corpus_docs
    payload["corpus_chunks"] = corpus_chunks
    return payload


def _print_summary(result, corpus_docs: int, corpus_chunks: int) -> None:  # noqa: ANN001
    print()
    print(f"Corpus:      {corpus_docs} docs / {corpus_chunks} chunks (disposable copy)")
    print(f"Queries:     {result.num_queries}  (K={result.k})")
    print(f"Raw keyword overlap@{result.k}:   {result.mean_raw_keyword_overlap_at_k:.3f}")
    print(f"Fused overlap@{result.k}:         {result.mean_fused_overlap_at_k:.3f}")
    print(f"Fused RBO (p={result.rbo_p}):       {result.mean_fused_rbo:.3f}")
    print(f"Top-K identical:        {result.pct_identical_topk:.1%} of queries")
    print(f"Recommendation:         {result.recommendation}")


async def run(args: argparse.Namespace) -> int:
    if not args.pg_dsn:
        print(
            "no Postgres DSN: pass --pg-dsn or set SAGE_EVAL_PG_DSN to a throwaway "
            "Postgres (the ts_rank arm needs a live server).",
            file=sys.stderr,
        )
        return 2

    queries = _load_queries(args.queries)

    config_path = config_path_for_vault(args.source_vault)
    if not config_path.exists():
        print(f"source vault config not found: {config_path}", file=sys.stderr)
        return 2
    config = load_vault_config(config_path)

    print(f"Loading services for source vault {args.source_vault!r} (read-only)...", flush=True)
    services = await initialize_services(
        config,
        config_path=config_path,
        abstraction_provider=StubAbstractionProvider(),
    )

    tmp_root = Path(tempfile.mkdtemp(prefix="kw_fidelity_"))
    pg: PostgresFTSBackend | None = None
    try:
        docs = await services.graph_store.list_all_documents()
        candidates = [(d.id, d.doc_type) for d in docs if d.doc_type]
        print(f"  {len(candidates)} typed documents available in {args.source_vault}")
        selected = _select_slice(candidates, args.corpus_size, args.corpus_seed)

        print(f"Copying {len(selected)} docs into a throwaway store at {tmp_root}...", flush=True)
        throwaway = LanceDBContentStore(tmp_root)
        embedding = services.retrieval_service._embedding
        pg_rows: list[tuple[str, str, str]] = []
        corpus_docs = 0
        for doc_id in selected:
            chunks = await services.content_store.get_all_chunks(doc_id)
            if not chunks:
                continue
            await throwaway.index_chunks(doc_id, chunks)
            corpus_docs += 1
            pg_rows.extend((c.document_id, c.heading_path, c.content) for c in chunks)
        print(f"  copied {corpus_docs} docs / {len(pg_rows)} chunks")

        print("Loading the same chunks into Postgres (ts_rank arm)...", flush=True)
        pg = PostgresFTSBackend(args.pg_dsn, table=args.pg_table, config=args.ts_config)
        loaded = pg.index(pg_rows)
        print(f"  loaded {loaded} rows into {args.pg_table}")

        fetch_limit = args.k * 5
        print(f"Running {len(queries)} queries (fetch {fetch_limit}/arm)...", flush=True)
        vector_rankings: dict[str, list] = {}
        bm25_rankings: dict[str, list] = {}
        ts_rankings: dict[str, list] = {}
        for q in queries:
            query_embedding = (await embedding.embed([q]))[0]
            vector_rankings[q] = await throwaway.search_semantic(query_embedding, fetch_limit)
            bm25_rankings[q] = await throwaway.search_bm25(q, fetch_limit)
            ts_rankings[q] = pg.search_bm25(q, fetch_limit)

        result = run_fidelity_eval(
            queries,
            vector_fn=lambda q: vector_rankings[q],
            bm25_fn=lambda q: bm25_rankings[q],
            ts_rank_fn=lambda q: ts_rankings[q],
            k=args.k,
        )

        output_dir = args.output_dir.expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        card_path = output_dir / f"{date}_keyword_fidelity_scorecard.md"
        raw_path = output_dir / f"{date}_keyword_fidelity_raw.json"
        card_path.write_text(render_scorecard(result), encoding="utf-8")
        raw_path.write_text(
            json.dumps(_serialize(result, corpus_docs, len(pg_rows)), indent=2),
            encoding="utf-8",
        )

        _print_summary(result, corpus_docs, len(pg_rows))
        print()
        print(f"Scorecard: {card_path}")
        print(f"Raw:       {raw_path}")
        return 0
    finally:
        if pg is not None:
            pg.close()
        await services.graph_store.close()
        shutil.rmtree(tmp_root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate keyword-backend fidelity (LanceDB BM25 vs Postgres ts_rank) "
            "through the production RRF. Reads a disposable slice of SOURCE_VAULT; "
            "makes no change to it."
        )
    )
    parser.add_argument("--source-vault", default="cas", help="Vault to slice the corpus from.")
    parser.add_argument("--corpus-size", type=int, default=40, help="Docs to copy (default 40).")
    parser.add_argument("--corpus-seed", type=int, default=42, help="Slice seed (default 42).")
    parser.add_argument("--k", type=int, default=10, help="Top-K depth to compare (default 10).")
    parser.add_argument(
        "--pg-dsn",
        default=os.environ.get("SAGE_EVAL_PG_DSN"),
        help="Postgres DSN (default: $SAGE_EVAL_PG_DSN).",
    )
    parser.add_argument("--pg-table", default="eval_chunks", help="Throwaway PG table name.")
    parser.add_argument("--ts-config", default="english", help="Postgres text-search config.")
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES, help="Query-set YAML.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Where to write the scorecard + raw JSON (default: {DEFAULT_OUTPUT_DIR}).",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
