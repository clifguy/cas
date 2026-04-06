"""LanceDB ContentStore implementation.

Embedded columnar vector database with native FTS support.
Stores document chunks with embeddings for semantic and keyword search.
"""

import logging
from pathlib import Path

import lancedb
import pyarrow as pa

from sage.adapters.interfaces import Chunk, ContentStore, SearchResult

logger = logging.getLogger(__name__)

# Fixed schema for the chunks table.
# Vector dimension must match the embedding provider (768 for nomic-embed-text).
VECTOR_DIMENSIONS = 768

CHUNKS_TABLE = "chunks"

CHUNKS_SCHEMA = pa.schema([
    pa.field("document_id", pa.utf8()),
    pa.field("heading_path", pa.utf8()),
    pa.field("content", pa.utf8()),
    pa.field("chunk_index", pa.int32()),
    pa.field("vector", pa.list_(pa.float32(), VECTOR_DIMENSIONS)),
])


class LanceDBContentStore(ContentStore):
    """Production content store backed by LanceDB.

    Data is persisted at brain_root on disk. The chunks table is created
    lazily on the first index_chunks call (AD-009). FTS index is rebuilt
    eagerly after every mutation (AD-019).
    """

    def __init__(self, brain_root: str | Path) -> None:
        self._brain_root = Path(brain_root)
        self._brain_root.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(self._brain_root / "lancedb"))
        self._table_exists = CHUNKS_TABLE in self._db.list_tables().tables

    def _get_table(self) -> lancedb.table.Table | None:
        """Return the chunks table, or None if it hasn't been created yet."""
        if not self._table_exists:
            # Recheck in case another call created it
            self._table_exists = CHUNKS_TABLE in self._db.list_tables().tables
        if self._table_exists:
            return self._db.open_table(CHUNKS_TABLE)
        return None

    def _ensure_table(self) -> lancedb.table.Table:
        """Return the chunks table, creating it if needed (AD-009)."""
        table = self._get_table()
        if table is not None:
            return table
        table = self._db.create_table(CHUNKS_TABLE, schema=CHUNKS_SCHEMA)
        self._table_exists = True
        logger.info("Created chunks table at %s", self._brain_root)
        return table

    def _rebuild_fts(self, table: lancedb.table.Table) -> None:
        """Rebuild the FTS index on the content column (AD-019).

        Called after every mutation to keep search results consistent.
        """
        try:
            table.create_fts_index("content", replace=True)
        except Exception:
            # FTS index creation can fail on empty tables; that's fine
            logger.debug("FTS index rebuild skipped (likely empty table)")

    async def index_chunks(self, document_id: str, chunks: list[Chunk]) -> None:
        """Store embedded chunks for a document.

        Replaces any existing chunks for the same document_id (AD-025).
        """
        table = self._ensure_table()

        # Remove existing chunks for this document first (AD-025)
        try:
            table.delete(f"document_id = '{_escape_sql(document_id)}'")
        except Exception:
            pass  # Table might be empty or document might not exist

        if not chunks:
            self._rebuild_fts(table)
            return

        # Build rows as list of dicts for LanceDB
        rows = []
        for chunk in chunks:
            embedding = chunk.embedding or [0.0] * VECTOR_DIMENSIONS
            rows.append({
                "document_id": chunk.document_id,
                "heading_path": chunk.heading_path,
                "content": chunk.content,
                "chunk_index": chunk.chunk_index,
                "vector": embedding,
            })

        table.add(rows)
        self._rebuild_fts(table)

    async def remove_document(self, document_id: str) -> None:
        """Remove all chunks for a document (AD-014, AD-015).

        Idempotent: removing a non-existent document is a no-op.
        """
        table = self._get_table()
        if table is None:
            return

        try:
            table.delete(f"document_id = '{_escape_sql(document_id)}'")
        except Exception:
            pass  # No rows to delete is fine

        self._rebuild_fts(table)

    async def search_semantic(
        self, query_embedding: list[float], limit: int = 10
    ) -> list[SearchResult]:
        """Vector similarity search using cosine distance (AD-016, AD-017).

        Returns results ranked by descending cosine similarity.
        """
        table = self._get_table()
        if table is None:
            return []

        try:
            results = (
                table.search(query_embedding, vector_column_name="vector")
                .metric("cosine")
                .limit(limit)
                .to_list()
            )
        except Exception as exc:
            logger.warning("Semantic search failed: %s", exc)
            return []

        return [
            SearchResult(
                document_id=row["document_id"],
                heading_path=row["heading_path"],
                content=row["content"],
                # LanceDB returns _distance (lower = more similar for cosine);
                # convert to similarity score (higher = more similar)
                score=1.0 - row.get("_distance", 0.0),
            )
            for row in results
        ]

    async def search_bm25(self, query: str, limit: int = 10) -> list[SearchResult]:
        """BM25 keyword search using LanceDB native FTS (AD-018, AD-019).

        Returns results ranked by relevance score, descending.
        """
        table = self._get_table()
        if table is None:
            return []

        try:
            results = (
                table.search(query, query_type="fts")
                .limit(limit)
                .to_list()
            )
        except Exception as exc:
            logger.warning("BM25 search failed: %s", exc)
            return []

        return [
            SearchResult(
                document_id=row["document_id"],
                heading_path=row["heading_path"],
                content=row["content"],
                score=row.get("_score", row.get("score", 0.0)),
            )
            for row in results
        ]

    async def get_chunks_by_heading_prefix(
        self, document_id: str, heading_prefix: str
    ) -> list[Chunk]:
        """Return chunks whose heading_path matches the prefix structurally (AD-020, AD-022).

        Matches exact heading or child headings via ' > ' separator.
        Does NOT match partial heading names (AD-022).
        """
        table = self._get_table()
        if table is None:
            return []

        escaped_doc = _escape_sql(document_id)
        escaped_prefix = _escape_sql(heading_prefix)
        # Escape SQL LIKE wildcards in the prefix
        like_prefix = _escape_like(heading_prefix)

        # Exact match OR child match via ' > ' separator
        filter_expr = (
            f"document_id = '{escaped_doc}' AND "
            f"(heading_path = '{escaped_prefix}' OR "
            f"heading_path LIKE '{like_prefix} > %')"
        )

        try:
            results = table.search().where(filter_expr).to_list()
        except Exception as exc:
            logger.warning("Heading prefix query failed: %s", exc)
            return []

        chunks = [
            Chunk(
                document_id=row["document_id"],
                heading_path=row["heading_path"],
                content=row["content"],
                embedding=row["vector"] if "vector" in row else None,
                chunk_index=row["chunk_index"],
            )
            for row in results
        ]
        chunks.sort(key=lambda c: c.chunk_index)
        return chunks

    async def get_all_chunks(self, document_id: str) -> list[Chunk]:
        """Return all chunks for a document in document order (AD-011, AD-013)."""
        table = self._get_table()
        if table is None:
            return []

        escaped_doc = _escape_sql(document_id)

        try:
            results = (
                table.search()
                .where(f"document_id = '{escaped_doc}'")
                .to_list()
            )
        except Exception as exc:
            logger.warning("get_all_chunks failed: %s", exc)
            return []

        chunks = [
            Chunk(
                document_id=row["document_id"],
                heading_path=row["heading_path"],
                content=row["content"],
                embedding=row["vector"] if "vector" in row else None,
                chunk_index=row["chunk_index"],
            )
            for row in results
        ]
        chunks.sort(key=lambda c: c.chunk_index)
        return chunks


def _escape_sql(value: str) -> str:
    """Escape single quotes for SQL string literals (AD-024)."""
    return value.replace("'", "''")


def _escape_like(value: str) -> str:
    """Escape SQL LIKE special characters and single quotes (AD-024).

    Escapes %, _, and ' to prevent incorrect matches or injection.
    """
    value = value.replace("'", "''")
    value = value.replace("%", "\\%")
    value = value.replace("_", "\\_")
    return value
