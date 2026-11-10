"""Vault teardown paths respect a running migrate_vault.

A running migration holds its vault's exclusion of pipeline work on the
vault's ingestion service, and its backfills write through the vault's
storage. Every path that stops that worker and closes that storage decides
what to do about it:

- An in-process ``delete_vault`` refuses before it destroys anything, and
  checks again at the eviction, since its own steps await.
- The listing's self-healing eviction leaves the vault in place and lets a
  later listing finish the job.
- A ``migrate_vault`` call that reaches services no longer installed (a
  reload replaced them, or an eviction removed them) runs on the installed
  services, or is refused with ``vault_not_found``. So a completed reload's
  teardown, and every eviction, never closes storage under a migration that
  started after the swap.

Process shutdown ends a running migration with the process; a re-call
resumes it, so it is not refused or awaited, and has no test here.
"""

import asyncio
import copy
import logging

import pytest

from sage.adapters.stubs import StubContentStore
from sage.api.errors import VaultNotFoundError
from sage.config import VaultConfig
from sage.maintenance.delete_vault import delete_vault
from sage.mcp_init import initialize_services
from sage.models.schemas import MigrationReport
from sage.services.vault_registry import VaultRegistryService
from sage.vault_source_binding import FilesystemVaultSourceStore
from tests.helpers.held_migration import hold_backfill_on, hold_migration, release_migration
from tests.helpers.pipeline_wait import drain_abstraction_queue
from tests.sage.conftest import initialize_services_for_test

pytestmark = pytest.mark.asyncio

VAULT = "test_vault"
REFUSED_MID_MIGRATION = 5


@pytest.fixture
async def registry(minimal_vault_config_dict, monkeypatch):
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    vaults: dict = {}
    registry_service = VaultRegistryService(vaults, initialize_services)
    async with initialize_services_for_test(
        config,
        registry_service=registry_service,
        content_store_factory=lambda _brain: StubContentStore(),
    ) as services:
        vaults[VAULT] = services
        try:
            yield registry_service, vaults
        finally:
            current = vaults.get(VAULT)
            if current is not None and current is not services:
                try:
                    await drain_abstraction_queue(current.ingestion_service)
                finally:
                    current.close_timing()
                    await current.close_storage()


async def _assert_serving_the_migration(services, vaults: dict) -> None:
    """The services stayed installed, open and visibly migrating."""
    assert vaults[VAULT] is services
    # A closed store raises on every dispatch, so a read proves it stayed open.
    assert isinstance(await services.graph_store.list_all_documents(), list)
    assert services.ingestion_service.migration_started_at is not None
    stats = await services.vault_config_service.get_stats()
    assert stats.migration_started_at is not None


# ---------------------------------------------------------------------------
# In-process delete_vault
# ---------------------------------------------------------------------------


class _Provisioner:
    """Records schema drops without a database; ``on_exists`` runs at the probe."""

    def __init__(self, on_exists=None) -> None:
        self.dropped: list[str] = []
        self._on_exists = on_exists

    async def schema_exists(self, vault_id: str) -> bool:
        if self._on_exists is not None:
            await self._on_exists()
        return True

    async def drop_vault_schema(self, vault_id: str) -> None:
        self.dropped.append(vault_id)


@pytest.fixture
def declared(tmp_path, minimal_vault_config_dict):
    """The vault's declaration on a filesystem vault-source store."""
    vault_root = tmp_path / "vaults"
    cfg = copy.deepcopy(minimal_vault_config_dict)
    cfg["vault"]["storage_root"] = str(vault_root / VAULT / "sources")
    cfg["vault"]["brain_root"] = str(vault_root / VAULT / "brain")
    store = FilesystemVaultSourceStore(vault_root)
    store.write_config(VAULT, cfg)
    return store, vault_root, tmp_path / "deletions"


def _delete_kwargs(declared, vaults: dict, provisioner: _Provisioner) -> dict:
    store, vault_root, snapshot_dir = declared
    return dict(
        vault_id=VAULT,
        source_store=store,
        provisioner=provisioner,
        vault_root=vault_root,
        snapshot_dir=snapshot_dir,
        reason="retire",
        apply=True,
        registry=vaults,
        input_fn=lambda _prompt: VAULT,
        dump_runner=lambda _argv: None,
    )


async def test_delete_refused_mid_migration_then_admitted(registry, declared, capsys):
    """Rival: an eviction that stops the worker and closes the storage the
    migration is writing through, then drops the schema under it."""
    _, vaults = registry
    services = vaults[VAULT]
    store, _, snapshot_dir = declared
    task, backfill, _ = await hold_migration(services)
    provisioner = _Provisioner()

    rc = await delete_vault(**_delete_kwargs(declared, vaults, provisioner))

    assert rc == REFUSED_MID_MIGRATION
    assert "migrate_vault is running" in capsys.readouterr().err
    assert provisioner.dropped == []
    # Refused before the snapshot and the audit record: nothing was written.
    assert not snapshot_dir.exists()
    assert store.config_locator(VAULT).exists()
    await _assert_serving_the_migration(services, vaults)

    await release_migration(task, backfill)
    rc = await delete_vault(**_delete_kwargs(declared, vaults, _Provisioner()))
    assert rc == 0
    assert VAULT not in vaults


async def test_delete_rechecks_at_the_eviction(registry, declared, capsys):
    """Rival: checking only at the start; the delete's own steps await, so a
    migration can begin between that check and the eviction."""
    _, vaults = registry
    services = vaults[VAULT]
    held: dict = {}

    async def migration_starts():
        held["task"], held["backfill"], _ = await hold_migration(services)

    provisioner = _Provisioner(on_exists=migration_starts)

    rc = await delete_vault(**_delete_kwargs(declared, vaults, provisioner))

    assert rc == REFUSED_MID_MIGRATION
    assert "migrate_vault is running" in capsys.readouterr().err
    assert provisioner.dropped == []
    await _assert_serving_the_migration(services, vaults)
    await release_migration(held["task"], held["backfill"])


async def test_delete_evicts_before_its_first_teardown_await(registry, declared):
    """Rival: dropping the registry entry only after the teardown awaits, so a
    migration resolved through the registry meanwhile still finds the vault."""
    _, vaults = registry
    services = vaults[VAULT]
    installed_at_stop: list = []
    stop_worker = services.ingestion_service.stop_worker

    async def recording_stop_worker(*args, **kwargs):
        installed_at_stop.append(VAULT in vaults)
        return await stop_worker(*args, **kwargs)

    services.ingestion_service.stop_worker = recording_stop_worker

    rc = await delete_vault(**_delete_kwargs(declared, vaults, _Provisioner()))

    assert rc == 0
    assert installed_at_stop == [False]


async def test_delete_finishes_the_teardown_when_a_step_fails(registry, declared):
    """Rival: stopping at the first failed teardown step after the entry is
    gone, so the storage stays open on services a re-run cannot reach."""
    _, vaults = registry
    services = vaults[VAULT]
    stop_worker = services.ingestion_service.stop_worker
    close_storage = services.close_storage
    closed: list[bool] = []

    async def failing_stop_worker(*args, **kwargs):
        await stop_worker(*args, **kwargs)
        raise RuntimeError("worker stop failed")

    async def recording_close_storage():
        closed.append(True)
        await close_storage()

    services.ingestion_service.stop_worker = failing_stop_worker
    services.close_storage = recording_close_storage
    provisioner = _Provisioner()

    rc = await delete_vault(**_delete_kwargs(declared, vaults, provisioner))

    assert rc == 4
    assert VAULT not in vaults
    assert closed == [True]
    assert provisioner.dropped == []
    # Control: the documented re-run completes the delete.
    assert await delete_vault(**_delete_kwargs(declared, vaults, provisioner)) == 0
    assert provisioner.dropped == [VAULT]


# ---------------------------------------------------------------------------
# The listing's self-healing eviction
# ---------------------------------------------------------------------------


def _vanish(registry_service, services, monkeypatch) -> None:
    """Make the vault's backing and declaration both read as gone."""

    async def absent(_vault_id):
        return False

    monkeypatch.setattr(services.graph_store, "storage_present", absent)
    monkeypatch.setattr(registry_service, "_discovered_vault_ids", lambda: set())


async def test_listing_defers_eviction_mid_migration(registry, monkeypatch, caplog):
    """Rivals: evicting a vanished vault whatever is running on it; leaving
    it in place only as a failed reconcile, logged as an error."""
    registry_service, vaults = registry
    services = vaults[VAULT]
    _vanish(registry_service, services, monkeypatch)
    task, backfill, _ = await hold_migration(services)

    with caplog.at_level(logging.WARNING, logger="sage.services.vault_registry"):
        listed = await registry_service.list_vaults()

    assert [v.id for v in listed] == []
    await _assert_serving_the_migration(services, vaults)
    messages = [r.getMessage() for r in caplog.records]
    assert any("eviction deferred" in m for m in messages)
    assert not any(m.startswith("Registry reconcile") for m in messages)

    await release_migration(task, backfill)
    await registry_service.list_vaults()
    assert VAULT not in vaults


# ---------------------------------------------------------------------------
# A migration that reaches services no longer installed
# ---------------------------------------------------------------------------


async def test_stale_migration_runs_on_the_installed_services(registry):
    """Rival: running on the services the caller resolved, which a completed
    reload has stopped and closed, and whose exclusion nothing now consults."""
    registry_service, vaults = registry
    old = vaults[VAULT]
    stale = old.maintenance_service

    await registry_service.reload_vault(VAULT)
    new = vaults[VAULT]
    assert new is not old
    backfill = hold_backfill_on(new)

    task = asyncio.create_task(stale.migrate_vault())
    await asyncio.wait_for(backfill.entered.wait(), timeout=5.0)

    assert new.ingestion_service.migration_started_at is not None
    assert old.ingestion_service.migration_started_at is None
    report = await release_migration(task, backfill)
    assert isinstance(report, MigrationReport)


async def test_migration_started_during_reload_teardown_lands_on_new_services(registry):
    """Rival: a migration that resolved the old services before the swap and
    starts while the old ones are torn down, closing storage under it."""
    registry_service, vaults = registry
    old = vaults[VAULT]
    stale = old.maintenance_service
    held: dict = {}
    stop_worker = old.ingestion_service.stop_worker

    async def migration_starts_during_teardown(*args, **kwargs):
        new = vaults[VAULT]
        held["backfill"] = hold_backfill_on(new)
        held["task"] = asyncio.create_task(stale.migrate_vault())
        await asyncio.wait_for(held["backfill"].entered.wait(), timeout=5.0)
        return await stop_worker(*args, **kwargs)

    old.ingestion_service.stop_worker = migration_starts_during_teardown

    await registry_service.reload_vault(VAULT)

    new = vaults[VAULT]
    assert new.ingestion_service.migration_started_at is not None
    assert old.ingestion_service.migration_started_at is None
    report = await release_migration(held["task"], held["backfill"])
    assert isinstance(report, MigrationReport)


async def test_stale_migration_after_eviction_is_vault_not_found(registry, monkeypatch):
    """Rival: running on evicted services, whose storage is closed."""
    registry_service, vaults = registry
    old = vaults[VAULT]
    stale = old.maintenance_service
    _vanish(registry_service, old, monkeypatch)
    await registry_service.list_vaults()
    assert VAULT not in vaults

    with pytest.raises(VaultNotFoundError) as excinfo:
        await stale.migrate_vault()

    assert excinfo.value.code == "vault_not_found"
    assert old.ingestion_service.migration_started_at is None
