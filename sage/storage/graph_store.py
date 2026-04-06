"""SQLite graph store for SAGE documents, edges, and users.

WAL mode enabled at startup (BH-004). All async methods use
asyncio.to_thread() to avoid blocking the event loop.
"""

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from sage.models.enums import EdgeType, PipelineStatus, SourceType, UserType
from sage.models.schemas import Document, Edge, User
from sage.storage.migrations import ALL_DDL


class GraphStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    async def initialize(self) -> None:
        """Create database, enable WAL mode (BH-004), run migrations."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        conn = self._get_connection()
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        for ddl in ALL_DDL:
            conn.execute(ddl)
        conn.commit()

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            conn = self._conn
            self._conn = None
            await asyncio.to_thread(conn.close)

    # ------------------------------------------------------------------
    # Document operations
    # ------------------------------------------------------------------

    async def insert_document(self, doc: Document) -> None:
        await asyncio.to_thread(self._insert_document_sync, doc)

    def _insert_document_sync(self, doc: Document) -> None:
        conn = self._get_connection()
        conn.execute(
            """INSERT INTO documents (
                id, title, source_type, source_path, lifecycle_status,
                version_label, project, tags, authority_scope, doc_type,
                source_content_hash, adapter_version, created_by, created_at,
                last_modified_by, updated_at, projected_at, indexed_at,
                semantic_abstract, pipeline_status, pipeline_error, tier3_metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                doc.semantic_abstract,
                doc.pipeline_status.value,
                doc.pipeline_error,
                json.dumps(doc.tier3_metadata) if doc.tier3_metadata else None,
            ),
        )
        conn.commit()

    async def get_document(self, doc_id: str) -> Document | None:
        return await asyncio.to_thread(self._get_document_sync, doc_id)

    def _get_document_sync(self, doc_id: str) -> Document | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_document(row)

    async def update_document(self, doc_id: str, updates: dict) -> Document | None:
        return await asyncio.to_thread(self._update_document_sync, doc_id, updates)

    def _update_document_sync(self, doc_id: str, updates: dict) -> Document | None:
        conn = self._get_connection()
        if not updates:
            return self._get_document_sync(doc_id)

        # Serialize JSON fields if present
        if "tags" in updates:
            updates["tags"] = json.dumps(updates["tags"])
        if "tier3_metadata" in updates:
            updates["tier3_metadata"] = json.dumps(updates["tier3_metadata"])

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        values.append(doc_id)
        conn.execute(
            f"UPDATE documents SET {set_clause} WHERE id = ?",
            values,
        )
        conn.commit()
        return self._get_document_sync(doc_id)

    async def find_by_source_path_and_hash(
        self, source_path: str, content_hash: str
    ) -> Document | None:
        return await asyncio.to_thread(
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
        return await asyncio.to_thread(self._find_by_source_path_sync, source_path)

    def _find_by_source_path_sync(self, source_path: str) -> list[Document]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM documents WHERE source_path = ?", (source_path,)
        ).fetchall()
        return [self._row_to_document(r) for r in rows]

    # ------------------------------------------------------------------
    # Edge operations
    # ------------------------------------------------------------------

    async def insert_edge(self, edge: Edge) -> None:
        await asyncio.to_thread(self._insert_edge_sync, edge)

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
        return await asyncio.to_thread(
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
        return await asyncio.to_thread(
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
    # User operations
    # ------------------------------------------------------------------

    async def insert_user(self, user: User) -> None:
        await asyncio.to_thread(self._insert_user_sync, user)

    def _insert_user_sync(self, user: User) -> None:
        conn = self._get_connection()
        conn.execute(
            "INSERT INTO users (id, display_name, type, created_at) VALUES (?, ?, ?, ?)",
            (user.id, user.display_name, user.type.value, user.created_at.isoformat()),
        )
        conn.commit()

    async def get_user(self, user_id: str) -> User | None:
        return await asyncio.to_thread(self._get_user_sync, user_id)

    def _get_user_sync(self, user_id: str) -> User | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    async def get_user_by_display_name(self, display_name: str) -> User | None:
        return await asyncio.to_thread(
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
        return await asyncio.to_thread(self._list_users_sync)

    def _list_users_sync(self) -> list[User]:
        conn = self._get_connection()
        rows = conn.execute("SELECT * FROM users").fetchall()
        return [self._row_to_user(r) for r in rows]

    # ------------------------------------------------------------------
    # Utility: query journal mode for BH-004
    # ------------------------------------------------------------------

    async def get_journal_mode(self) -> str:
        return await asyncio.to_thread(self._get_journal_mode_sync)

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
            semantic_abstract=row["semantic_abstract"],
            pipeline_status=PipelineStatus(row["pipeline_status"]),
            pipeline_error=row["pipeline_error"],
            tier3_metadata=(
                json.loads(row["tier3_metadata"])
                if row["tier3_metadata"]
                else None
            ),
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
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            display_name=row["display_name"],
            type=UserType(row["type"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
