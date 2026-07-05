"""``measured_byte_size`` across ContentStore bindings + get_stats delegation.

The on-disk-size stat moved off a binding-specific directory walk in
``VaultConfigService.get_stats`` onto the ``ContentStore`` port, so every
binding reports its own footprint and the service stays substrate-agnostic.
These cover the Stub implementation and the service delegation; the Postgres
implementation is covered in ``test_content_store_postgres.py``. No Postgres
server required.
"""

from __future__ import annotations

from sage.adapters.stubs import StubContentStore
from sage.services.vault_config import VaultConfigService


async def test_stub_measured_byte_size_is_zero():
    """The in-memory stub has no on-disk footprint."""
    assert await StubContentStore().measured_byte_size() == 0


async def test_get_stats_reads_size_from_port(graph_store, minimal_config):
    """get_stats sources content_store_size_bytes from the port, not the disk.

    A stub vault has no on-disk footprint, so the retired directory walk
    would have yielded 0; the sentinel proves the value comes from
    ``content_store.measured_byte_size()``."""

    class SentinelStore(StubContentStore):
        async def measured_byte_size(self) -> int:
            return 4242

    service = VaultConfigService(graph_store, SentinelStore(), minimal_config, None)
    stats = await service.get_stats()
    assert stats.content_store_size_bytes == 4242
