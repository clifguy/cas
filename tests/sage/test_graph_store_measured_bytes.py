"""``measured_byte_size`` at the GraphStore port + get_stats delegation.

Mirrors ``test_content_store_measured_bytes.py``: the graph store's size stat
lives on the ``GraphStore`` port, so every binding reports its own live
footprint and ``VaultConfigService.get_stats`` stays substrate-agnostic.
These cover the Stub binding and the service delegation; the Postgres
implementation is covered in ``test_postgres_graph_store.py``.
"""

from __future__ import annotations

from sage.adapters.stubs import StubContentStore, StubGraphStore
from sage.services.vault_config import VaultConfigService


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
