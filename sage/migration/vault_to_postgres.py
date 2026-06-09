"""Copy a vault's derived/curated state into Postgres and reconcile it (CAS-ADR-042).

The migration reads a vault's documents, edges, staging edges, users, chunks
(with embeddings), and abstracts through the ``GraphStore`` / ``ContentStore``
ports and writes equivalent rows into a Postgres schema through the Postgres
implementations of the same ports. It then reconciles the copy against the
source and returns a structured report covering document/edge identity, hash
matches, chain-head invariants, chunk and vector fidelity, and abstract
coverage.

Two halves, deliberately separated for testability:

- **Pure reconciliation** (``reconcile_*``) compares already-fetched records and
  builds the report. No I/O, so it is exhaustively unit-testable without a
  database.
- **Orchestration** (``migrate_vault``) wires the source reads, the ordered
  Postgres load, and the reconciliation together.

Load order is load-bearing. ``is_chain_head`` is not a model field; it is a
storage column the Postgres chain-head trigger maintains, flipping a
predecessor to ``false`` when a ``supersedes`` edge targets it. So the load
inserts every document first (the column defaults ``true``), then every edge
(the trigger reconstructs chain-head state from the edge set), and only then
creates the per-vault tier3 partial-unique indexes — which require correct
chain-head state to validate without a spurious predecessor/head collision.

Idempotency is truncate-then-load: the target tables are reset (and any
pre-existing tier3 partial-unique indexes dropped) before each run, so a
re-run against the same schema reproduces the same rows rather than colliding
on a primary key. The source stores are only ever read; they are never mutated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, computed_field

from sage.models.enums import EdgeType
from sage.storage.migrations import Tier3UniqueIndexBlockedError

if TYPE_CHECKING:
    from sage.adapters.interfaces import ContentStore, GraphStore
    from sage.config import VaultConfig
    from sage.models.schemas import Document, Edge

# The tables a vault's state lands in, in a TRUNCATE-safe order (CASCADE handles
# the foreign keys between them). Mirrors the storage schema's table set.
_TABLE_NAMES: tuple[str, ...] = (
    "documents",
    "edges",
    "staging_edges",
    "users",
    "document_tags",
    "chunks",
)

# Float tolerance for embedding-component equality across the LanceDB float32 ->
# pgvector float4 round-trip. Both are single precision, so a faithful copy
# round-trips well within this bound; a dropped/zeroed vector does not.
_VECTOR_TOLERANCE = 1e-5


# ---------------------------------------------------------------------------
# Report models
# ---------------------------------------------------------------------------


class HashMismatch(BaseModel):
    """A document whose source content hash differs between source and target."""

    document_id: str
    source_hash: str
    target_hash: str


class DocumentReconciliation(BaseModel):
    """Document-set comparison: counts, membership, and per-field fidelity."""

    source_count: int
    target_count: int
    missing: list[str] = []  # in source, absent in target
    extra: list[str] = []  # in target, absent in source
    field_mismatch: list[str] = []  # present both sides, differing model fields
    hash_mismatch: list[HashMismatch] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        return (
            self.source_count == self.target_count
            and not self.missing
            and not self.extra
            and not self.field_mismatch
            and not self.hash_mismatch
        )


class EdgeFieldMismatch(BaseModel):
    """A single edge field that differs between source and target (e.g. an anchor)."""

    edge_id: str
    field: str
    source_value: str | None
    target_value: str | None


class EdgeReconciliation(BaseModel):
    """Edge-set comparison. Ids are preserved on copy, so this is identity equality."""

    source_count: int
    target_count: int
    missing: list[str] = []
    extra: list[str] = []
    field_mismatch: list[EdgeFieldMismatch] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        return (
            self.source_count == self.target_count
            and not self.missing
            and not self.extra
            and not self.field_mismatch
        )


class ChainHeadReconciliation(BaseModel):
    """Per-document ``is_chain_head`` agreement between source and target."""

    source_count: int
    target_count: int
    divergent: list[str] = []  # ids where the chain-head flag disagrees or is one-sided

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        return self.source_count == self.target_count and not self.divergent


class ChunkCountMismatch(BaseModel):
    """A document whose chunk row count differs between source and target."""

    document_id: str
    source_count: int
    target_count: int


class ChunkReconciliation(BaseModel):
    """Chunk completeness (per-document counts) and embedding fidelity."""

    source_total: int
    target_total: int
    per_doc_mismatch: list[ChunkCountMismatch] = []
    vector_mismatch: list[str] = []  # document ids whose embeddings did not round-trip

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        return (
            self.source_total == self.target_total
            and not self.per_doc_mismatch
            and not self.vector_mismatch
        )


class AbstractReconciliation(BaseModel):
    """Semantic-abstract coverage parity (a dropped abstract is invisible to counts)."""

    total_documents: int
    source_coverage: int
    target_coverage: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        return self.source_coverage == self.target_coverage

    @computed_field  # type: ignore[prop-decorator]
    @property
    def coverage_ratio(self) -> float:
        if self.total_documents == 0:
            return 1.0
        return self.target_coverage / self.total_documents


class IdSetReconciliation(BaseModel):
    """Id-set equality for collections compared by identity alone (users, staging)."""

    source_count: int
    target_count: int
    missing: list[str] = []
    extra: list[str] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        return self.source_count == self.target_count and not self.missing and not self.extra


class Tier3IndexBlocked(BaseModel):
    """A tier3 uniqueness constraint the substrate refused to activate post-load."""

    doc_type: str
    field: str
    message: str


class VaultMigrationReport(BaseModel):
    """The per-vault reconciliation report; ``ok`` gates cutover.

    ``ok`` is the conjunction of every sub-reconciliation plus the absence of
    blocked tier3 indexes. It is only meaningful for an executed run; a dry-run
    report carries source counts with an empty target and is not a pass/fail
    verdict.
    """

    vault_id: str
    executed: bool
    documents: DocumentReconciliation
    edges: EdgeReconciliation
    chain_heads: ChainHeadReconciliation
    chunks: ChunkReconciliation
    abstracts: AbstractReconciliation
    users: IdSetReconciliation
    staging_edges: IdSetReconciliation
    tier3_indexes_created: list[str] = []
    tier3_indexes_blocked: list[Tier3IndexBlocked] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        return (
            self.executed
            and self.documents.ok
            and self.edges.ok
            and self.chain_heads.ok
            and self.chunks.ok
            and self.abstracts.ok
            and self.users.ok
            and self.staging_edges.ok
            and not self.tier3_indexes_blocked
        )


# ---------------------------------------------------------------------------
# Pure reconciliation
# ---------------------------------------------------------------------------

# Edge fields compared for identity equality (everything that carries provenance
# or resolution semantics). ``id`` keys the comparison, so it is excluded here.
_EDGE_COMPARE_FIELDS: tuple[str, ...] = (
    "source_id",
    "target_id",
    "edge_type",
    "resolution_policy",
    "source_valid_from_version",
    "target_valid_from_version",
    "valid_until_version",
    "retracted_edge_id",
    "created_at",
    "notes",
    "rationale",
    "rationale_kind",
    "synced_from_version",
    "synced_from_content_hash",
)


def reconcile_documents(source: list[Document], target: list[Document]) -> DocumentReconciliation:
    """Compare two document sets by id, full-field equality, and content hash."""
    src_by_id = {d.id: d for d in source}
    tgt_by_id = {d.id: d for d in target}
    missing = sorted(src_by_id.keys() - tgt_by_id.keys())
    extra = sorted(tgt_by_id.keys() - src_by_id.keys())
    field_mismatch: list[str] = []
    hash_mismatch: list[HashMismatch] = []
    for did in sorted(src_by_id.keys() & tgt_by_id.keys()):
        s, t = src_by_id[did], tgt_by_id[did]
        if s.source_content_hash != t.source_content_hash:
            hash_mismatch.append(
                HashMismatch(
                    document_id=did,
                    source_hash=s.source_content_hash,
                    target_hash=t.source_content_hash,
                )
            )
        if s.model_dump() != t.model_dump():
            field_mismatch.append(did)
    return DocumentReconciliation(
        source_count=len(source),
        target_count=len(target),
        missing=missing,
        extra=extra,
        field_mismatch=field_mismatch,
        hash_mismatch=hash_mismatch,
    )


def reconcile_edges(source: list[Edge], target: list[Edge]) -> EdgeReconciliation:
    """Compare two edge sets by id and per-field equality (anchors, provenance)."""
    src_by_id = {e.id: e for e in source}
    tgt_by_id = {e.id: e for e in target}
    missing = sorted(src_by_id.keys() - tgt_by_id.keys())
    extra = sorted(tgt_by_id.keys() - src_by_id.keys())
    field_mismatch: list[EdgeFieldMismatch] = []
    for eid in sorted(src_by_id.keys() & tgt_by_id.keys()):
        s_dump = src_by_id[eid].model_dump()
        t_dump = tgt_by_id[eid].model_dump()
        for field in _EDGE_COMPARE_FIELDS:
            if s_dump.get(field) != t_dump.get(field):
                field_mismatch.append(
                    EdgeFieldMismatch(
                        edge_id=eid,
                        field=field,
                        source_value=_stringify(s_dump.get(field)),
                        target_value=_stringify(t_dump.get(field)),
                    )
                )
    return EdgeReconciliation(
        source_count=len(source),
        target_count=len(target),
        missing=missing,
        extra=extra,
        field_mismatch=field_mismatch,
    )


def reconcile_chain_heads(
    source: dict[str, bool], target: dict[str, bool]
) -> ChainHeadReconciliation:
    """Compare per-document ``is_chain_head`` maps; any disagreement is divergent."""
    divergent = sorted(
        did for did in source.keys() | target.keys() if source.get(did) != target.get(did)
    )
    return ChainHeadReconciliation(
        source_count=len(source),
        target_count=len(target),
        divergent=divergent,
    )


def reconcile_chunks(
    source_counts: dict[str, int],
    target_counts: dict[str, int],
    *,
    vector_mismatch: list[str] | None = None,
) -> ChunkReconciliation:
    """Compare per-document chunk counts and (when provided) vector fidelity."""
    per_doc_mismatch: list[ChunkCountMismatch] = []
    for did in sorted(source_counts.keys() | target_counts.keys()):
        s = source_counts.get(did, 0)
        t = target_counts.get(did, 0)
        if s != t:
            per_doc_mismatch.append(
                ChunkCountMismatch(document_id=did, source_count=s, target_count=t)
            )
    return ChunkReconciliation(
        source_total=sum(source_counts.values()),
        target_total=sum(target_counts.values()),
        per_doc_mismatch=per_doc_mismatch,
        vector_mismatch=sorted(vector_mismatch or []),
    )


def reconcile_abstracts(source: list[Document], target: list[Document]) -> AbstractReconciliation:
    """Compare semantic-abstract coverage (count of non-null abstracts)."""
    return AbstractReconciliation(
        total_documents=len(source),
        source_coverage=sum(1 for d in source if d.semantic_abstract),
        target_coverage=sum(1 for d in target if d.semantic_abstract),
    )


def reconcile_id_sets(source_ids: set[str], target_ids: set[str]) -> IdSetReconciliation:
    """Compare two id sets for membership equality (users, staging edges)."""
    return IdSetReconciliation(
        source_count=len(source_ids),
        target_count=len(target_ids),
        missing=sorted(source_ids - target_ids),
        extra=sorted(target_ids - source_ids),
    )


def _stringify(value: object) -> str | None:
    return None if value is None else str(value)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def migrate_vault(
    *,
    source_graph: GraphStore,
    source_content: ContentStore,
    target_graph: GraphStore,
    target_content: ContentStore,
    target_pool: object,
    config: VaultConfig,
    vault_id: str,
    execute: bool,
) -> VaultMigrationReport:
    """Migrate one vault's state into the target Postgres schema and reconcile it.

    ``target_pool`` is the opened async connection pool the target stores share;
    it is bound to the destination schema by the caller and used here for the
    raw-SQL probes the ports do not express (chain-head column, embedding
    vectors, the truncate reset). The schema is assumed already provisioned
    (tables present); this routine resets and loads it.

    When ``execute`` is false, nothing is written: the report carries the source
    counts against an empty target as a pre-flight, with ``executed`` false.
    """
    # --- Read source state (never mutated) ---
    source_docs = await source_graph.list_all_documents()
    source_users = await source_graph.list_users()
    source_staging = await source_graph.list_staging_edges()
    source_edges = await _collect_edges(source_graph, source_docs)

    # Source chain-head truth is implied by the supersedes edge set: a document
    # is a chain head iff no supersedes edge targets it (the same rule the
    # storage trigger encodes).
    superseded = {
        e.target_id
        for e in source_edges
        if e.edge_type == EdgeType.SUPERSEDES and e.target_id is not None
    }
    source_chain_heads = {d.id: d.id not in superseded for d in source_docs}

    if execute:
        await _reset_target(target_pool)
        await target_graph.initialize()
        for user in source_users:
            await target_graph.insert_user(user)
        for doc in source_docs:
            await target_graph.insert_document(doc)
        for edge in source_edges:
            await target_graph.insert_edge(edge, on_conflict="raise")
        for staging in source_staging:
            await target_graph.insert_staging_edge(staging, on_conflict="raise")

    # --- Chunk copy + per-document count/vector capture (streamed per doc) ---
    source_chunk_counts: dict[str, int] = {}
    target_chunk_counts: dict[str, int] = {}
    vector_mismatch: list[str] = []
    for doc in source_docs:
        chunks = await source_content.get_all_chunks(doc.id)
        source_chunk_counts[doc.id] = len(chunks)
        if execute:
            await target_content.index_chunks(doc.id, chunks)
            target_chunk_counts[doc.id] = len(await target_content.get_all_chunks(doc.id))
            source_vecs = {(c.heading_path, c.chunk_index): c.embedding for c in chunks}
            target_vecs = await _target_vector_map(target_pool, doc.id)
            if not _vector_maps_match(source_vecs, target_vecs):
                vector_mismatch.append(doc.id)

    # --- Tier3 partial-unique indexes (created last, on correct chain-head state) ---
    tier3_created: list[str] = []
    tier3_blocked: list[Tier3IndexBlocked] = []
    if execute:
        tier3_created, tier3_blocked = await _create_tier3_indexes(target_graph, config)

    # --- Fetch target state back through the ports for reconciliation ---
    if execute:
        target_docs = await target_graph.list_all_documents()
        target_users = await target_graph.list_users()
        target_staging = await target_graph.list_staging_edges()
        target_edges = await _collect_edges(target_graph, target_docs)
        target_chain_heads = await _target_chain_head_map(target_pool)
    else:
        target_docs = []
        target_users = []
        target_staging = []
        target_edges = []
        target_chain_heads = {}

    return VaultMigrationReport(
        vault_id=vault_id,
        executed=execute,
        documents=reconcile_documents(source_docs, target_docs),
        edges=reconcile_edges(source_edges, target_edges),
        chain_heads=reconcile_chain_heads(source_chain_heads, target_chain_heads),
        chunks=reconcile_chunks(
            source_chunk_counts, target_chunk_counts, vector_mismatch=vector_mismatch
        ),
        abstracts=reconcile_abstracts(source_docs, target_docs),
        users=reconcile_id_sets({u.id for u in source_users}, {u.id for u in target_users}),
        staging_edges=reconcile_id_sets(
            {s.id for s in source_staging}, {s.id for s in target_staging}
        ),
        tier3_indexes_created=tier3_created,
        tier3_indexes_blocked=tier3_blocked,
    )


async def _collect_edges(graph: GraphStore, docs: list[Document]) -> list[Edge]:
    """Enumerate every edge in the vault as raw ``Edge`` objects.

    Each edge has exactly one source document, so iterating every document and
    collecting its outbound edges yields the full edge set exactly once. This is
    the only enumeration that returns ``Edge`` (``query_edges`` returns rows with
    computed retraction state, which ``insert_edge`` cannot consume).
    """
    edges: list[Edge] = []
    seen: set[str] = set()
    for doc in docs:
        for edge in await graph.get_edges_by_source(doc.id):
            if edge.id not in seen:
                seen.add(edge.id)
                edges.append(edge)
    return edges


async def _create_tier3_indexes(
    target_graph: GraphStore, config: VaultConfig
) -> tuple[list[str], list[Tier3IndexBlocked]]:
    """Create each declared tier3 partial-unique index on the target.

    Mirrors the substrate's uniqueness-activation walk: for every
    ``(doc_type, field)`` named in the vault's ``unique_keys`` declarations,
    create the index. A genuine collision surfaces as ``Tier3IndexBlocked``
    rather than being swallowed, because a blocked constraint means the migrated
    chain-head portfolio is not unique where the vault declares it must be.
    """
    created: list[str] = []
    blocked: list[Tier3IndexBlocked] = []
    for dt in config.document_types.doc_types:
        if not dt.unique_keys:
            continue
        for field in dt.unique_keys:
            try:
                await target_graph.ensure_tier3_unique_index(dt.value, field)
                created.append(f"{dt.value}.{field}")
            except Tier3UniqueIndexBlockedError as exc:
                blocked.append(Tier3IndexBlocked(doc_type=dt.value, field=field, message=str(exc)))
    return created, blocked


# ---------------------------------------------------------------------------
# Raw-SQL probes (target only; the ports do not surface these)
# ---------------------------------------------------------------------------


async def _reset_target(pool: object) -> None:
    """Truncate the target tables and drop any tier3 partial-unique indexes.

    The truncate is the idempotency reset: ``insert_document`` raises on a
    primary-key collision, so a re-run needs a clean slate. The tier3 indexes
    are dropped because ``TRUNCATE`` leaves them in place and they are recreated
    from the vault config after load.
    """
    async with pool.connection() as conn:  # type: ignore[attr-defined]
        async with conn.transaction():
            await conn.execute(f"TRUNCATE {', '.join(_TABLE_NAMES)} CASCADE")  # noqa: S608
            cur = await conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND indexname LIKE 'idx_tier3_unique_%'"
            )
            for (idxname,) in await cur.fetchall():
                await conn.execute(f'DROP INDEX IF EXISTS "{idxname}"')  # noqa: S608


async def _target_chain_head_map(pool: object) -> dict[str, bool]:
    """Read the actual ``is_chain_head`` column for every target document."""
    async with pool.connection() as conn:  # type: ignore[attr-defined]
        cur = await conn.execute("SELECT id, is_chain_head FROM documents")
        rows = await cur.fetchall()
    return {row[0]: bool(row[1]) for row in rows}


async def _target_vector_map(pool: object, document_id: str) -> dict[tuple[str, int], object]:
    """Read the stored embedding for each of a document's chunks, keyed by position."""
    async with pool.connection() as conn:  # type: ignore[attr-defined]
        cur = await conn.execute(
            "SELECT heading_path, chunk_index, embedding FROM chunks WHERE document_id = %s",
            (document_id,),
        )
        rows = await cur.fetchall()
    return {(row[0], row[1]): row[2] for row in rows}


def _vector_maps_match(
    source: dict[tuple[str, int], object], target: dict[tuple[str, int], object]
) -> bool:
    """Return True if both chunk-vector maps cover the same keys with equal vectors."""
    if source.keys() != target.keys():
        return False
    for key, s_vec in source.items():
        t_vec = target[key]
        if s_vec is None and t_vec is None:
            continue
        if s_vec is None or t_vec is None:
            return False
        s_list = list(s_vec)
        t_list = list(t_vec)
        if len(s_list) != len(t_list):
            return False
        if any(abs(float(a) - float(b)) > _VECTOR_TOLERANCE for a, b in zip(s_list, t_list)):
            return False
    return True
