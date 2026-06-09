"""``measured_byte_size`` across ContentStore bindings + get_stats delegation.

The on-disk-size stat moved off a binding-specific directory walk in
``VaultConfigService.get_stats`` onto the ``ContentStore`` port, so every
binding reports its own footprint and the service stays substrate-agnostic.
These cover the LanceDB and Stub implementations and the service delegation;
the Postgres implementation is covered in ``test_content_store_postgres.py``.
No Postgres server required.
"""

from __future__ import annotations

from sage.adapters.content_store_lancedb import VECTOR_DIMENSIONS, LanceDBContentStore
from sage.adapters.interfaces import Chunk
from sage.adapters.stubs import StubContentStore
from sage.services.vault_config import VaultConfigService


def _chunk(document_id: str, *, content: str, chunk_index: int = 0) -> Chunk:
    return Chunk(
        document_id=document_id,
        heading_path="Section",
        content=content,
        embedding=[0.0] * VECTOR_DIMENSIONS,
        chunk_index=chunk_index,
    )


async def test_lancedb_measured_byte_size_matches_directory_walk(tmp_path):
    """The LanceDB binding reports the recursive byte sum of its directory."""
    store = LanceDBContentStore(tmp_path)
    lancedb_dir = tmp_path / "lancedb"

    def walk() -> int:
        if not lancedb_dir.exists():
            return 0
        return sum(f.stat().st_size for f in lancedb_dir.rglob("*") if f.is_file())

    before = await store.measured_byte_size()
    assert before == walk()  # tracks the directory walk, not a hardcoded constant

    await store.index_chunks(
        "doc_1",
        [
            _chunk("doc_1", content="alpha bravo charlie"),
            _chunk("doc_1", content="delta", chunk_index=1),
        ],
    )

    after = await store.measured_byte_size()
    assert after == walk()
    assert after > before


async def test_stub_measured_byte_size_is_zero():
    """The in-memory stub has no on-disk footprint."""
    assert await StubContentStore().measured_byte_size() == 0


async def test_get_stats_reads_size_from_port(graph_store, minimal_config):
    """get_stats sources content_store_size_bytes from the port, not the disk.

    A stub vault has no LanceDB directory, so the retired directory walk would
    have yielded 0; the sentinel proves the value comes from
    ``content_store.measured_byte_size()``."""

    class SentinelStore(StubContentStore):
        async def measured_byte_size(self) -> int:
            return 4242

    service = VaultConfigService(graph_store, SentinelStore(), minimal_config, None)
    stats = await service.get_stats()
    assert stats.content_store_size_bytes == 4242
