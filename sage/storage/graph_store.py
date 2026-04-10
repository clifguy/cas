"""SQLite graph store for SAGE documents, edges, and users.

WAL mode enabled per-connection (BH-004). Each thread in the executor
pool gets its own connection via threading.local(), allowing concurrent
reads under WAL mode while SQLite serializes writes internally.
"""

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from sage.models.enums import EdgeType, PipelineStatus, SourceType, UserType
from sage.models.schemas import Document, Edge, StagingEdge, User
from sage.storage.migrations import MIGRATIONS, POST_MIGRATION_DDL, TABLES

T = TypeVar("T")


class GraphStore:
    def __init__(self, db_path: Path, max_connections: int = 4) -> None:
        self._db_path = db_path
        self._max_connections = max_connections
        self._executor: ThreadPoolExecutor | None = None
        self._local = threading.local()
        self._all_connections: list[sqlite3.Connection] = []
        self._all_connections_lock = threading.Lock()

    def _get_connection(self) -> sqlite3.Connection:
        """Return the thread-local connection, creating one if needed."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            self._local.conn = conn
            with self._all_connections_lock:
                self._all_connections.append(conn)
        return conn

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        """Dispatch a sync callable to the connection-pool executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    async def initialize(self) -> None:
        """Create database, start executor pool, run migrations."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_connections,
            thread_name_prefix="sage-graph",
        )
        await self._run(self._initialize_sync)

    def _initialize_sync(self) -> None:
        conn = self._get_connection()
        # 1. Create tables (IF NOT EXISTS -- no-op for existing databases)
        for ddl in TABLES:
            conn.execute(ddl)
        # 2. Apply ALTER TABLE migrations so new columns exist before indexes
        for migration in MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # Column already exists
        # 3. Create indexes (may reference columns added by migrations)
        for ddl in POST_MIGRATION_DDL:
            conn.execute(ddl)
        conn.commit()

    async def close(self) -> None:
        if self._executor is not None:
            executor = self._executor
            self._executor = None
            executor.shutdown(wait=True)
        with self._all_connections_lock:
            for conn in self._all_connections:
                conn.close()
            self._all_connections.clear()

    # ------------------------------------------------------------------
    # Document operations
    # ------------------------------------------------------------------

    async def insert_document(self, doc: Document) -> None:
        await self._run(self._insert_document_sync, doc)

    def _insert_document_sync(self, doc: Document) -> None:
        conn = self._get_connection()
        conn.execute(
            """INSERT INTO documents (
                id, title, source_type, source_path, lifecycle_status,
                version_label, project, tags, authority_scope, doc_type,
                source_content_hash, adapter_version, created_by, created_at,
                last_modified_by, updated_at, projected_at, indexed_at,
                source_modified_at,
                semantic_abstract, pipeline_status, pipeline_error, tier3_metadata,
                metadata_confirmed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc.id,
                doc.title,
                doc.source_type.value,
                doc.source_path,
                doc.lifecycle_status,
                doc.version_label,
                doc.project,
                json.dumps(doc.tags),
                doc.authority_scope,
                doc.doc_type,
                doc.source_content_hash,
                doc.adapter_version,
                doc.created_by,
                doc.created_at.isoformat(),
                doc.last_modified_by,
                doc.updated_at.isoformat(),
                doc.projected_at.isoformat() if doc.projected_at else None,
                doc.indexed_at.isoformat() if doc.indexed_at else None,
                doc.source_modified_at.isoformat() if doc.source_modified_at else None,
                doc.semantic_abstract,
                doc.pipeline_status.value,
                doc.pipeline_error,
                json.dumps(doc.tier3_metadata) if doc.tier3_metadata else None,
                1 if doc.metadata_confirmed else 0,
            ),
        )
        conn.commit()

    async def get_document(self, doc_id: str) -> Document | None:
        return await self._run(self._get_document_sync, doc_id)

    def _get_document_sync(self, doc_id: str) -> Document | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_document(row)

    async def update_document(self, doc_id: str, updates: dict) -> Document | None:
        return await self._run(self._update_document_sync, doc_id, updates)

    def _update_document_sync(self, doc_id: str, updates: dict) -> Document | None:
        conn = self._get_connection()
        if not updates:
            return self._get_document_sync(doc_id)

        # Serialize JSON fields if present
        if "tags" in updates:
            updates["tags"] = json.dumps(updates["tags"])
        if "tier3_metadata" in updates:
            updates["tier3_metadata"] = json.dumps(updates["tier3_metadata"])
        if "metadata_confirmed" in updates:
            updates["metadata_confirmed"] = 1 if updates["metadata_confirmed"] else 0

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        values.append(doc_id)
        conn.execute(
            f"UPDATE documents SET {set_clause} WHERE id = ?",
            values,
        )
        conn.commit()
        return self._get_document_sync(doc_id)

    async def list_all_documents(self) -> list[Document]:
        """Return all documents in the graph store."""
        return await self._run(self._list_all_documents_sync)

    def _list_all_documents_sync(self) -> list[Document]:
        conn = self._get_connection()
        rows = conn.execute("SELECT * FROM documents").fetchall()
        return [self._row_to_document(r) for r in rows]

    async def find_by_source_path_and_hash(
        self, source_path: str, content_hash: str
    ) -> Document | None:
        return await self._run(
            self._find_by_source_path_and_hash_sync, source_path, content_hash
        )

    def _find_by_source_path_and_hash_sync(
        self, source_path: str, content_hash: str
    ) -> Document | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM documents WHERE source_path = ? AND source_content_hash = ?",
            (source_path, content_hash),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_document(row)

    async def find_by_source_path(self, source_path: str) -> list[Document]:
        return await self._run(self._find_by_source_path_sync, source_path)

    def _find_by_source_path_sync(self, source_path: str) -> list[Document]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM documents WHERE source_path = ?", (source_path,)
        ).fetchall()
        return [self._row_to_document(r) for r in rows]

    async def find_documents_by_title(self, title: str) -> list[Document]:
        """Find all documents with a matching title (case-insensitive)."""
        return await self._run(self._find_documents_by_title_sync, title)

    def _find_documents_by_title_sync(self, title: str) -> list[Document]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM documents WHERE LOWER(title) = LOWER(?)",
            (title,),
        ).fetchall()
        return [self._row_to_document(r) for r in rows]

    async def search_metadata(self, query: str, limit: int = 20) -> list[Document]:
        """Search documents by substring match in title, source_path, and tags.

        Returns documents where the query appears in any identity field,
        ordered by source_path match first (most specific), then title,
        then tags. Case-insensitive.
        """
        return await self._run(self._search_metadata_sync, query, limit)

    def _search_metadata_sync(self, query: str, limit: int) -> list[Document]:
        conn = self._get_connection()
        pattern = f"%{query}%"
        rows = conn.execute(
            "SELECT * FROM documents "
            "WHERE source_path LIKE ? COLLATE NOCASE "
            "   OR title LIKE ? COLLATE NOCASE "
            "   OR tags LIKE ? COLLATE NOCASE "
            "ORDER BY "
            "  CASE WHEN source_path LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END, "
            "  CASE WHEN title LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END "
            "LIMIT ?",
            (pattern, pattern, pattern, pattern, pattern, limit),
        ).fetchall()
        return [self._row_to_document(r) for r in rows]

    # ------------------------------------------------------------------
    # Edge operations
    # ------------------------------------------------------------------

    async def insert_edge(self, edge: Edge) -> None:
        await self._run(self._insert_edge_sync, edge)

    def _insert_edge_sync(self, edge: Edge) -> None:
        conn = self._get_connection()
        conn.execute(
            """INSERT INTO edges (id, source_id, target_id, edge_type, created_at, notes, rationale)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                edge.id,
                edge.source_id,
                edge.target_id,
                edge.edge_type.value,
                edge.created_at.isoformat(),
                edge.notes,
                edge.rationale,
            ),
        )
        conn.commit()

    async def get_edges_by_source(
        self, source_id: str, edge_type: str | None = None
    ) -> list[Edge]:
        return await self._run(
            self._get_edges_by_source_sync, source_id, edge_type
        )

    def _get_edges_by_source_sync(
        self, source_id: str, edge_type: str | None = None
    ) -> list[Edge]:
        conn = self._get_connection()
        if edge_type:
            rows = conn.execute(
                "SELECT * FROM edges WHERE source_id = ? AND edge_type = ?",
                (source_id, edge_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM edges WHERE source_id = ?", (source_id,)
            ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    async def get_edges_by_target(
        self, target_id: str, edge_type: str | None = None
    ) -> list[Edge]:
        return await self._run(
            self._get_edges_by_target_sync, target_id, edge_type
        )

    def _get_edges_by_target_sync(
        self, target_id: str, edge_type: str | None = None
    ) -> list[Edge]:
        conn = self._get_connection()
        if edge_type:
            rows = conn.execute(
                "SELECT * FROM edges WHERE target_id = ? AND edge_type = ?",
                (target_id, edge_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM edges WHERE target_id = ?", (target_id,)
            ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    # ------------------------------------------------------------------
    # Hash check (BE-007)
    # ------------------------------------------------------------------

    async def find_documents_by_hashes(
        self, hashes: list[str]
    ) -> dict[str, str]:
        """Return {hash: document_id} for hashes that exist in the store."""
        return await self._run(self._find_documents_by_hashes_sync, hashes)

    def _find_documents_by_hashes_sync(self, hashes: list[str]) -> dict[str, str]:
        if not hashes:
            return {}
        conn = self._get_connection()
        placeholders = ",".join("?" for _ in hashes)
        rows = conn.execute(
            f"SELECT source_content_hash, id FROM documents "
            f"WHERE source_content_hash IN ({placeholders})",
            hashes,
        ).fetchall()
        return {row["source_content_hash"]: row["id"] for row in rows}

    # ------------------------------------------------------------------
    # Staging edge operations (BE-010 through BE-013)
    # ------------------------------------------------------------------

    async def list_staging_edges(self) -> list[StagingEdge]:
        return await self._run(self._list_staging_edges_sync)

    def _list_staging_edges_sync(self) -> list[StagingEdge]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM staging_edges ORDER BY created_at"
        ).fetchall()
        return [self._row_to_staging_edge(r) for r in rows]

    async def get_staging_edge(self, edge_id: str) -> StagingEdge | None:
        return await self._run(self._get_staging_edge_sync, edge_id)

    def _get_staging_edge_sync(self, edge_id: str) -> StagingEdge | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM staging_edges WHERE id = ?", (edge_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_staging_edge(row)

    async def insert_staging_edge(self, edge: StagingEdge) -> None:
        await self._run(self._insert_staging_edge_sync, edge)

    def _insert_staging_edge_sync(self, edge: StagingEdge) -> None:
        conn = self._get_connection()
        conn.execute(
            """INSERT INTO staging_edges
            (id, source_id, target_id, edge_type, inference_evidence,
             confidence_tier, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                edge.id,
                edge.source_id,
                edge.target_id,
                edge.edge_type.value,
                edge.inference_evidence,
                edge.confidence_tier,
                edge.created_at.isoformat(),
            ),
        )
        conn.commit()

    async def delete_staging_edge(self, edge_id: str) -> bool:
        """Delete a staging edge. Returns True if it existed."""
        return await self._run(self._delete_staging_edge_sync, edge_id)

    def _delete_staging_edge_sync(self, edge_id: str) -> bool:
        conn = self._get_connection()
        cursor = conn.execute(
            "DELETE FROM staging_edges WHERE id = ?", (edge_id,)
        )
        conn.commit()
        return cursor.rowcount > 0

    async def count_staging_edges(self) -> int:
        return await self._run(self._count_staging_edges_sync)

    def _count_staging_edges_sync(self) -> int:
        conn = self._get_connection()
        row = conn.execute("SELECT COUNT(*) FROM staging_edges").fetchone()
        return row[0]

    # ------------------------------------------------------------------
    # Statistics (BE-003 through BE-005)
    # ------------------------------------------------------------------

    async def get_document_counts_by_field(
        self, field: str
    ) -> dict[str, int]:
        """Return {value: count} for a given document column."""
        return await self._run(self._get_document_counts_by_field_sync, field)

    def _get_document_counts_by_field_sync(self, field: str) -> dict[str, int]:
        # Allowlist of valid fields to prevent SQL injection
        allowed = {"lifecycle_status", "doc_type", "source_type", "pipeline_status"}
        if field not in allowed:
            return {}
        conn = self._get_connection()
        rows = conn.execute(
            f"SELECT {field}, COUNT(*) as cnt FROM documents "
            f"WHERE {field} IS NOT NULL GROUP BY {field}"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    async def get_edge_counts_by_type(self) -> dict[str, int]:
        return await self._run(self._get_edge_counts_by_type_sync)

    def _get_edge_counts_by_type_sync(self) -> dict[str, int]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT edge_type, COUNT(*) as cnt FROM edges GROUP BY edge_type"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    async def get_total_document_count(self) -> int:
        return await self._run(self._get_total_document_count_sync)

    def _get_total_document_count_sync(self) -> int:
        conn = self._get_connection()
        return conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    async def get_total_edge_count(self) -> int:
        return await self._run(self._get_total_edge_count_sync)

    def _get_total_edge_count_sync(self) -> int:
        conn = self._get_connection()
        return conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    async def get_last_ingestion_at(self) -> datetime | None:
        return await self._run(self._get_last_ingestion_at_sync)

    def _get_last_ingestion_at_sync(self) -> datetime | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT MAX(created_at) FROM documents"
        ).fetchone()
        if row[0] is None:
            return None
        return datetime.fromisoformat(row[0])

    async def count_documents_by_pipeline_status(
        self, status: str
    ) -> int:
        return await self._run(
            self._count_documents_by_pipeline_status_sync, status
        )

    def _count_documents_by_pipeline_status_sync(self, status: str) -> int:
        conn = self._get_connection()
        return conn.execute(
            "SELECT COUNT(*) FROM documents WHERE pipeline_status = ?",
            (status,),
        ).fetchone()[0]

    # ------------------------------------------------------------------
    # Pending metadata (BE-014)
    # ------------------------------------------------------------------

    async def list_pending_metadata_documents(self) -> list[Document]:
        """Return documents where metadata_confirmed is false."""
        return await self._run(self._list_pending_metadata_documents_sync)

    def _list_pending_metadata_documents_sync(self) -> list[Document]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM documents WHERE metadata_confirmed = 0"
        ).fetchall()
        return [self._row_to_document(r) for r in rows]

    # ------------------------------------------------------------------
    # Graph traversal
    # ------------------------------------------------------------------

    async def traverse(
        self,
        start_id: str,
        edge_type: str | None,
        direction: str,
        depth: int,
    ) -> list[dict]:
        """Recursive CTE traversal returning raw dicts for service-layer dedup."""
        return await self._run(
            self._traverse_sync, start_id, edge_type, direction, depth
        )

    def _traverse_sync(
        self,
        start_id: str,
        edge_type: str | None,
        direction: str,
        depth: int,
    ) -> list[dict]:
        conn = self._get_connection()

        # Build direction-specific column references:
        #   outbound: follow source_id -> target_id
        #   inbound:  follow target_id -> source_id
        #   both:     union of outbound and inbound
        def _edge_select(from_col: str, to_col: str, alias: str = "seed") -> str:
            """SQL fragment selecting edges in one direction."""
            type_filter = " AND e.edge_type = ?" if edge_type else ""
            return (
                f"SELECT e.id AS edge_id, e.{to_col} AS doc_id, "
                f"e.edge_type, e.created_at AS edge_created_at, "
                f"e.notes, e.rationale, e.source_id, e.target_id, "
                f"1 AS depth "
                f"FROM edges e "
                f"WHERE e.{from_col} = ?{type_filter}"
            )

        def _recursive_step(from_col: str, to_col: str) -> str:
            type_filter = " AND e.edge_type = ?" if edge_type else ""
            return (
                f"SELECT e.id AS edge_id, e.{to_col} AS doc_id, "
                f"e.edge_type, e.created_at AS edge_created_at, "
                f"e.notes, e.rationale, e.source_id, e.target_id, "
                f"t.depth + 1 AS depth "
                f"FROM edges e "
                f"INNER JOIN traversal t ON e.{from_col} = t.doc_id "
                f"WHERE t.depth < ?{type_filter}"
            )

        params: list = []

        if direction == "outbound":
            seed = _edge_select("source_id", "target_id")
            params += [start_id] + ([edge_type] if edge_type else [])
            recurse = _recursive_step("source_id", "target_id")
            params += [depth] + ([edge_type] if edge_type else [])
        elif direction == "inbound":
            seed = _edge_select("target_id", "source_id")
            params += [start_id] + ([edge_type] if edge_type else [])
            recurse = _recursive_step("target_id", "source_id")
            params += [depth] + ([edge_type] if edge_type else [])
        else:  # both
            seed_out = _edge_select("source_id", "target_id")
            seed_in = _edge_select("target_id", "source_id")
            seed = f"{seed_out} UNION ALL {seed_in}"
            params += [start_id] + ([edge_type] if edge_type else [])
            params += [start_id] + ([edge_type] if edge_type else [])
            recurse_out = _recursive_step("source_id", "target_id")
            recurse_in = _recursive_step("target_id", "source_id")
            recurse = f"{recurse_out} UNION ALL {recurse_in}"
            params += [depth] + ([edge_type] if edge_type else [])
            params += [depth] + ([edge_type] if edge_type else [])

        sql = (
            f"WITH RECURSIVE traversal AS (\n"
            f"  {seed}\n"
            f"  UNION ALL\n"
            f"  {recurse}\n"
            f")\n"
            f"SELECT t.*, "
            f"d.id AS d_id, d.title, d.lifecycle_status, d.source_type, "
            f"d.version_label, d.project, d.doc_type, d.tags "
            f"FROM traversal t "
            f"INNER JOIN documents d ON t.doc_id = d.id"
        )

        rows = conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            results.append({
                "edge_id": row["edge_id"],
                "doc_id": row["doc_id"],
                "edge_type": row["edge_type"],
                "edge_created_at": row["edge_created_at"],
                "notes": row["notes"],
                "rationale": row["rationale"],
                "source_id": row["source_id"],
                "target_id": row["target_id"],
                "depth": row["depth"],
                "d_title": row["title"],
                "d_lifecycle_status": row["lifecycle_status"],
                "d_source_type": row["source_type"],
                "d_version_label": row["version_label"],
                "d_project": row["project"],
                "d_doc_type": row["doc_type"],
                "d_tags": row["tags"],
            })
        return results

    # ------------------------------------------------------------------
    # User operations
    # ------------------------------------------------------------------

    async def insert_user(self, user: User) -> None:
        await self._run(self._insert_user_sync, user)

    def _insert_user_sync(self, user: User) -> None:
        conn = self._get_connection()
        conn.execute(
            "INSERT INTO users (id, display_name, type, created_at) VALUES (?, ?, ?, ?)",
            (user.id, user.display_name, user.type.value, user.created_at.isoformat()),
        )
        conn.commit()

    async def get_user(self, user_id: str) -> User | None:
        return await self._run(self._get_user_sync, user_id)

    def _get_user_sync(self, user_id: str) -> User | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    async def get_user_by_display_name(self, display_name: str) -> User | None:
        return await self._run(
            self._get_user_by_display_name_sync, display_name
        )

    def _get_user_by_display_name_sync(self, display_name: str) -> User | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM users WHERE display_name = ?", (display_name,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    async def list_users(self) -> list[User]:
        return await self._run(self._list_users_sync)

    def _list_users_sync(self) -> list[User]:
        conn = self._get_connection()
        rows = conn.execute("SELECT * FROM users").fetchall()
        return [self._row_to_user(r) for r in rows]

    # ------------------------------------------------------------------
    # Utility: query journal mode for BH-004
    # ------------------------------------------------------------------

    async def get_journal_mode(self) -> str:
        return await self._run(self._get_journal_mode_sync)

    def _get_journal_mode_sync(self) -> str:
        conn = self._get_connection()
        row = conn.execute("PRAGMA journal_mode;").fetchone()
        return row[0]

    # ------------------------------------------------------------------
    # Row -> model conversions
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_document(row: sqlite3.Row) -> Document:
        return Document(
            id=row["id"],
            title=row["title"],
            source_type=SourceType(row["source_type"]),
            source_path=row["source_path"],
            lifecycle_status=row["lifecycle_status"],
            version_label=row["version_label"],
            project=row["project"],
            tags=json.loads(row["tags"]) if row["tags"] else [],
            authority_scope=row["authority_scope"],
            doc_type=row["doc_type"],
            source_content_hash=row["source_content_hash"],
            adapter_version=row["adapter_version"],
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_modified_by=row["last_modified_by"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
            projected_at=(
                datetime.fromisoformat(row["projected_at"])
                if row["projected_at"]
                else None
            ),
            indexed_at=(
                datetime.fromisoformat(row["indexed_at"])
                if row["indexed_at"]
                else None
            ),
            source_modified_at=(
                datetime.fromisoformat(row["source_modified_at"])
                if row["source_modified_at"]
                else None
            ),
            semantic_abstract=row["semantic_abstract"],
            pipeline_status=PipelineStatus(row["pipeline_status"]),
            pipeline_error=row["pipeline_error"],
            tier3_metadata=(
                json.loads(row["tier3_metadata"])
                if row["tier3_metadata"]
                else None
            ),
            metadata_confirmed=bool(row["metadata_confirmed"]),
        )

    @staticmethod
    def _row_to_edge(row: sqlite3.Row) -> Edge:
        return Edge(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            edge_type=EdgeType(row["edge_type"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            notes=row["notes"],
            rationale=row["rationale"],
        )

    @staticmethod
    def _row_to_staging_edge(row: sqlite3.Row) -> StagingEdge:
        return StagingEdge(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            edge_type=EdgeType(row["edge_type"]),
            inference_evidence=row["inference_evidence"],
            confidence_tier=row["confidence_tier"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            display_name=row["display_name"],
            type=UserType(row["type"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
