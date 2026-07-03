"""``measured_byte_size`` across GraphStore bindings + get_stats delegation.

Mirrors ``test_content_store_measured_bytes.py``: the graph store's on-disk
size stat moves off a Postgres-blind file walk in ``VaultConfigService.get_stats``
onto the ``GraphStore`` port, so every binding reports its own live footprint
and the service stays substrate-agnostic. These cover the SQLite and Stub
implementations and the service delegation; the Postgres implementation is
covered in ``test_postgres_graph_store.py``. No Postgres server required.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sage.adapters.stubs import StubContentStore, StubGraphStore
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document
from sage.services.vault_config import VaultConfigService


def _sha(name: str) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(name.encode()).hexdigest()}"


def _make_doc(doc_id: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Doc {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        source_content_hash=_sha(doc_id),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )


def _graph_db_bytes(sqlite_graph_store) -> int:
    """Sum of graph.db and any WAL/SHM siblings, independent of the store."""
    db_path = sqlite_graph_store._db_path  # noqa: SLF001 -- test reaches into internals deliberately
    total = 0
    for path in (
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ):
        if path.exists():
            total += path.stat().st_size
    return total


async def test_sqlite_graph_store_measured_byte_size_matches_file_walk(sqlite_graph_store):
    """The SQLite binding reports the byte sum of graph.db plus WAL/SHM siblings."""
    before = await sqlite_graph_store.measured_byte_size()
    assert before == _graph_db_bytes(sqlite_graph_store)  # tracks the file walk, not a constant

    await sqlite_graph_store.insert_document(_make_doc("00000001_doc_1"))

    after = await sqlite_graph_store.measured_byte_size()
    assert after == _graph_db_bytes(sqlite_graph_store)
    assert after > before


async def test_stub_graph_store_measured_byte_size_is_zero():
    """The in-memory stub has no on-disk footprint."""
    assert await StubGraphStore().measured_byte_size() == 0


async def test_get_stats_reads_graph_size_from_port(graph_store, minimal_config):
    """get_stats sources graph_store_size_bytes from the port, not the disk.

    Monkeypatches ``measured_byte_size`` to a sentinel value on the real
    (initialized) ``graph_store`` fixture -- proving the value flows through
    the port rather than being recomputed from a fixed file/table stat, the
    same way the ContentStore sentinel test proves content-store delegation.
    """

    async def _sentinel() -> int:
        return 4242

    graph_store.measured_byte_size = _sentinel

    service = VaultConfigService(graph_store, StubContentStore(), minimal_config, None)
    stats = await service.get_stats()
    assert stats.graph_store_size_bytes == 4242
