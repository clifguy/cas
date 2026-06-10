#!/usr/bin/env python3
"""Evaluate the keyword backend: LanceDB BM25 vs Postgres ``ts_rank``.

Builds a disposable corpus by copying a representative slice of an existing
SAGE vault's chunks (with their stored embeddings) into a throwaway LanceDB
content store, and loads the identical chunk text into a throwaway Postgres
table. For each query the harness fetches three rankings -- the vector ranking
(the constant arm), LanceDB BM25 (keyword arm A), and Postgres ``ts_rank``
(keyword arm B) -- and fuses each keyword arm with the shared vector arm through
the production Reciprocal Rank Fusion.

Two evaluation modes share that machinery (``--mode``, default ``relevance``):

* ``relevance`` grades *absolute* answer quality of each arm against a judged,
  keyword-skewed gold set (``--gold``) using NDCG@k, MRR, and success@k. Gold
  targets are pinned into the disposable slice so each judged answer is present.
  Output is a per-arm scorecard plus a go/no-go recommendation: keep native
  ``ts_rank`` if it stays within tolerance of LanceDB BM25, else escalate.
* ``divergence`` runs the original arm-vs-arm agreement eval (overlap@k, RBO).

The eval never queries the source vault: it copies a slice out and runs entirely
against the throwaway store and table, which it owns end-to-end and tears down on
exit. It reads the source vault only through SAGE's own content-store adapter and
makes no change to it.

Requires ``psycopg`` (a base dependency since the Postgres storage binding) and a
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
    ARM_BM25,
    ARM_TSRANK,
    ARM_VECTOR,
    DEFAULT_REL_DELTA,
    GoldQuery,
    PostgresFTSBackend,
    parse_gold_entry,
    render_relevance_scorecard,
    render_scorecard,
    run_fidelity_eval,
    run_relevance_eval,
)
from sage.vault_management import config_path_for_vault

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUERIES = REPO_ROOT / "tests" / "fixtures" / "keyword_eval_queries.yaml"
DEFAULT_GOLD = REPO_ROOT / "tests" / "fixtures" / "keyword_eval_gold.yaml"
DEFAULT_OUTPUT_DIR = Path.home() / "sage_eval_output"

# Evaluation modes. ``relevance`` (default) grades absolute answer quality of
# each arm against the judged gold set; ``divergence`` runs the original
# arm-vs-arm agreement eval; ``both`` runs each in turn.
MODE_RELEVANCE = "relevance"
MODE_DIVERGENCE = "divergence"
MODE_BOTH = "both"


def _load_queries(path: Path) -> list[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [str(q) for q in data["queries"]]


def load_gold(path: Path) -> list[GoldQuery]:
    """Load the judged gold set (query + stable selectors) from YAML."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [parse_gold_entry(entry) for entry in data["queries"]]


def _resolve_gold(docs: list, gold: list[GoldQuery]) -> list[GoldQuery]:  # noqa: ANN001
    """Resolve each gold selector to a stable document id.

    A selector is either ``adr:<id>`` -- the ADR carrying that tier3 ``adr_id``,
    which outlives re-versioning and so is the version-proof handle for an ADR
    target -- or a document ``source_path``. When a selector resolves to more
    than one lifecycle state, the active version wins. A selector that resolves
    to no document raises: a stale gold set must fail loudly, not score against
    nothing.
    """
    by_path: dict[str, list] = {}
    by_adr: dict[str, list] = {}
    for doc in docs:
        if doc.source_path:
            by_path.setdefault(doc.source_path, []).append(doc)
        adr_id = (doc.tier3_metadata or {}).get("adr_id")
        if adr_id is not None:
            by_adr.setdefault(str(adr_id), []).append(doc)

    def resolve(selector: str) -> str:
        if selector.startswith("adr:"):
            matches = by_adr.get(selector[len("adr:") :], [])
        else:
            matches = by_path.get(selector, [])
        if not matches:
            raise ValueError(f"gold selector did not resolve to any document: {selector!r}")
        active = [d for d in matches if d.lifecycle_status == "active"]
        return (active or matches)[0].id

    return [
        GoldQuery(
            query=gq.query,
            primary_id=resolve(gq.primary_id),
            relevant_ids=[resolve(rid) for rid in gq.relevant_ids],
        )
        for gq in gold
    ]


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


def pin_and_select(
    candidates: list[tuple[str, str]],
    pinned_ids: list[str],
    size: int,
    seed: int,
) -> list[str]:
    """Select a slice of ``size`` doc ids that always includes every pinned id.

    The gold targets (``pinned_ids``) are force-included first; the remainder up
    to ``size`` is filled by the stratified round-robin ``_select_slice`` over the
    not-yet-selected candidates. So a judged target is present in the disposable
    corpus even for a (seed, size) the sampler alone would not have reached.
    Raises if a pinned id is absent from the candidate corpus -- a stale gold set
    must fail loudly rather than silently score against a missing target.
    """
    pinned = list(dict.fromkeys(pinned_ids))  # dedup, order-preserving
    candidate_ids = {doc_id for doc_id, _ in candidates}
    missing = [pid for pid in pinned if pid not in candidate_ids]
    if missing:
        raise ValueError(f"pinned gold target(s) not in candidate corpus: {missing}")
    pinned_set = set(pinned)
    remaining = [(doc_id, dt) for doc_id, dt in candidates if doc_id not in pinned_set]
    fill = _select_slice(remaining, max(0, size - len(pinned)), seed)
    return [*pinned, *fill]


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


def _print_relevance_summary(result, corpus_docs: int, corpus_chunks: int) -> None:  # noqa: ANN001
    print()
    print(f"Corpus:       {corpus_docs} docs / {corpus_chunks} chunks (disposable copy)")
    print(f"Gold queries: {result.num_queries}  (K={result.k}, delta={result.delta:.3f})")
    for arm in (ARM_VECTOR, ARM_BM25, ARM_TSRANK):
        a = result.arms[arm]
        print(
            f"  {arm:13s} NDCG@{result.k}={a.mean_ndcg_at_k:.3f}  "
            f"MRR={a.mean_reciprocal_rank:.3f}  "
            f"success@{result.k}={a.mean_success_at_k:.3f}"
        )
    print(f"Recommendation: {result.recommendation}")


async def run(args: argparse.Namespace) -> int:
    if not args.pg_dsn:
        print(
            "no Postgres DSN: pass --pg-dsn or set SAGE_EVAL_PG_DSN to a throwaway "
            "Postgres (the ts_rank arm needs a live server).",
            file=sys.stderr,
        )
        return 2

    mode = args.mode
    div_queries = _load_queries(args.queries) if mode in (MODE_DIVERGENCE, MODE_BOTH) else []
    gold = load_gold(args.gold) if mode in (MODE_RELEVANCE, MODE_BOTH) else []

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

        resolved_gold = _resolve_gold(docs, gold) if gold else []
        pinned_ids: list[str] = []
        for gq in resolved_gold:
            pinned_ids.append(gq.primary_id)
            pinned_ids.extend(gq.relevant_ids)
        if pinned_ids:
            selected = pin_and_select(candidates, pinned_ids, args.corpus_size, args.corpus_seed)
            print(f"  pinned {len(set(pinned_ids))} gold target(s) into the slice")
        else:
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

        all_queries = list(dict.fromkeys([*div_queries, *(gq.query for gq in resolved_gold)]))
        fetch_limit = args.k * 5
        print(f"Running {len(all_queries)} queries (fetch {fetch_limit}/arm)...", flush=True)
        vector_rankings: dict[str, list] = {}
        bm25_rankings: dict[str, list] = {}
        ts_rankings: dict[str, list] = {}
        for q in all_queries:
            query_embedding = (await embedding.embed([q]))[0]
            vector_rankings[q] = await throwaway.search_semantic(query_embedding, fetch_limit)
            bm25_rankings[q] = await throwaway.search_bm25(q, fetch_limit)
            ts_rankings[q] = pg.search_bm25(q, fetch_limit)

        def vfn(q: str) -> list:
            return vector_rankings[q]

        def bfn(q: str) -> list:
            return bm25_rankings[q]

        def tfn(q: str) -> list:
            return ts_rankings[q]

        output_dir = args.output_dir.expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        written: list[Path] = []

        if div_queries:
            result = run_fidelity_eval(div_queries, vfn, bfn, tfn, k=args.k)
            card_path = output_dir / f"{date}_keyword_fidelity_scorecard.md"
            raw_path = output_dir / f"{date}_keyword_fidelity_raw.json"
            card_path.write_text(render_scorecard(result), encoding="utf-8")
            raw_path.write_text(
                json.dumps(_serialize(result, corpus_docs, len(pg_rows)), indent=2),
                encoding="utf-8",
            )
            _print_summary(result, corpus_docs, len(pg_rows))
            written += [card_path, raw_path]

        if resolved_gold:
            rel = run_relevance_eval(resolved_gold, vfn, bfn, tfn, k=args.k, delta=args.delta)
            rel_card = output_dir / f"{date}_keyword_relevance_scorecard.md"
            rel_raw = output_dir / f"{date}_keyword_relevance_raw.json"
            rel_card.write_text(render_relevance_scorecard(rel), encoding="utf-8")
            rel_raw.write_text(
                json.dumps(_serialize(rel, corpus_docs, len(pg_rows)), indent=2),
                encoding="utf-8",
            )
            _print_relevance_summary(rel, corpus_docs, len(pg_rows))
            written += [rel_card, rel_raw]

        print()
        for path in written:
            print(f"Wrote: {path}")
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
        "--mode",
        choices=(MODE_RELEVANCE, MODE_DIVERGENCE, MODE_BOTH),
        default=MODE_RELEVANCE,
        help="Which eval to run (default: relevance).",
    )
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD, help="Judged gold-set YAML.")
    parser.add_argument(
        "--delta",
        type=float,
        default=DEFAULT_REL_DELTA,
        help=f"Relevance adequacy tolerance (default {DEFAULT_REL_DELTA}).",
    )
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
