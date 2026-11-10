"""A vault is not reloaded while migrate_vault runs on it.

The migration's exclusion of pipeline work lives on the vault's ingestion
service. A reload replaces that service and closes the storage the migration
holds, so a reload mid-migration would admit pipeline work against a vault
still being migrated, report no migration in flight, and close storage under
the running backfills. ``reload_vault`` and ``update_vault_config`` (which
reloads after writing) therefore refuse with ``vault_migration_in_flight``
while a migration holds the vault, and proceed once it returns. The decision
is taken below both surfaces, so REST and MCP refuse identically.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

import sage.mcp_init as _mcp_init
import sage.mcp_server as _mcp
from sage.adapters.stubs import StubContentStore
from sage.api.errors import VaultMigrationInFlightError
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.mcp_server import reload_vault as mcp_reload_vault
from sage.mcp_server import update_vault_config as mcp_update_vault_config
from sage.services.vault_registry import VaultRegistryService
from tests.helpers.pipeline_wait import drain_abstraction_queue, drain_vaults
from tests.sage.conftest import initialize_services_for_test

pytestmark = pytest.mark.asyncio

RENAMED = "Renamed While Migrating"


class _HeldBackfill:
    """Stands in for one migration backfill and holds the migration open."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def __call__(self) -> int:
        self.entered.set()
        await self.gate.wait()
        return 0


async def _hold_migration(services) -> tuple[asyncio.Task, _HeldBackfill, datetime]:
    """Run a real ``migrate_vault`` on ``services`` and hold it mid-run."""
    backfill = _HeldBackfill()
    services.maintenance_service._divide_passages_over_input_bound = backfill
    started = datetime.now(timezone.utc)
    task = asyncio.create_task(services.maintenance_service.migrate_vault())
    await asyncio.wait_for(backfill.entered.wait(), timeout=5.0)
    return task, backfill, started


async def _release(task: asyncio.Task, backfill: _HeldBackfill) -> None:
    backfill.gate.set()
    await asyncio.wait_for(task, timeout=5.0)


def _assert_refusal(code: str, detail: dict, vault_id: str, started: datetime) -> None:
    assert code == "vault_migration_in_flight"
    assert detail["vault_id"] == vault_id
    start_time = datetime.fromisoformat(detail["start_time"])
    assert started <= start_time <= datetime.now(timezone.utc)


async def _assert_untouched(old, registry: dict, vault_id: str) -> None:
    """The refused call left the migrating services installed and serving."""
    assert registry[vault_id] is old
    # A closed store raises on every dispatch, so a read proves it stayed open.
    assert isinstance(await old.graph_store.list_all_documents(), list)
    stats = await old.vault_config_service.get_stats()
    assert stats.migration_started_at is not None
    assert old.ingestion_service.migration_started_at is not None


def _vault_section(tmp_vault_dir, name: str) -> dict:
    return {
        "id": "test_vault",
        "name": name,
        "owner": "testuser",
        "storage_root": str(tmp_vault_dir / "sources"),
        "brain_root": str(tmp_vault_dir / "brain"),
        "visibility": "personal",
    }


@pytest.fixture
def config_writes(monkeypatch) -> list[str]:
    """Record every vault-config write that reaches the vault-source store."""
    writes: list[str] = []
    resolve = _mcp_init.resolve_stack_vault_source_store

    def spying_resolve(*args, **kwargs):
        store = resolve(*args, **kwargs)
        write = store.write_config

        def recording_write(vault_id, config):
            writes.append(vault_id)
            return write(vault_id, config)

        store.write_config = recording_write
        return store

    monkeypatch.setattr(_mcp_init, "resolve_stack_vault_source_store", spying_resolve)
    return writes


# ---------------------------------------------------------------------------
# Service layer: one decision for every caller of the reload path
# ---------------------------------------------------------------------------


@pytest.fixture
async def registry(minimal_vault_config_dict, tmp_vault_dir, monkeypatch):
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    vaults: dict = {}
    registry_service = VaultRegistryService(vaults, initialize_services)
    async with initialize_services_for_test(
        config,
        registry_service=registry_service,
        content_store_factory=lambda _brain: StubContentStore(),
    ) as services:
        vaults["test_vault"] = services
        try:
            yield registry_service, vaults
        finally:
            current = vaults.get("test_vault")
            if current is not None and current is not services:
                try:
                    await drain_abstraction_queue(current.ingestion_service)
                finally:
                    current.close_timing()
                    await current.close_storage()


async def test_service_reload_refused_mid_migration_then_admitted(registry, monkeypatch):
    """Rivals: a reload that consults nothing and swaps the services; one that
    builds replacement services (opening storage) before it refuses."""
    registry_service, vaults = registry
    old = vaults["test_vault"]
    builds: list = []

    async def counting_initialize_services(*args, **kwargs):
        builds.append(args)
        return await initialize_services(*args, **kwargs)

    monkeypatch.setattr(_mcp_init, "initialize_services", counting_initialize_services)
    task, backfill, started = await _hold_migration(old)

    with pytest.raises(VaultMigrationInFlightError) as excinfo:
        await registry_service.reload_vault("test_vault")

    _assert_refusal(excinfo.value.code, excinfo.value.detail, "test_vault", started)
    assert excinfo.value.status_code == 409
    assert builds == []
    await _assert_untouched(old, vaults, "test_vault")

    await _release(task, backfill)
    result = await registry_service.reload_vault("test_vault")
    assert result.reloaded is True
    assert vaults["test_vault"] is not old


async def test_a_reload_installs_the_new_services_before_tearing_down_the_old(registry):
    """Rival: tearing the old services down first, which leaves the registry
    serving them while their worker stops and their storage closes, after the
    last point at which a starting migration could be refused."""
    registry_service, vaults = registry
    old = vaults["test_vault"]
    installed_at_teardown: list = []
    stop_worker = old.ingestion_service.stop_worker

    async def recording_stop_worker(*args, **kwargs):
        installed_at_teardown.append(vaults["test_vault"])
        return await stop_worker(*args, **kwargs)

    old.ingestion_service.stop_worker = recording_stop_worker

    await registry_service.reload_vault("test_vault")

    assert len(installed_at_teardown) == 1
    assert installed_at_teardown[0] is not old
    assert installed_at_teardown[0] is vaults["test_vault"]


async def test_a_migration_that_starts_during_the_rebuild_is_not_torn_down(registry, monkeypatch):
    """Rival: checking only before the new services are built.

    Building awaits, so a migration can begin on the old services while the
    reload is constructing their replacement. The reload must notice before it
    tears the old services down, and must not leak what it built.
    """
    registry_service, vaults = registry
    old = vaults["test_vault"]
    held: dict = {}
    built: list = []

    async def building_while_a_migration_starts(*args, **kwargs):
        held["task"], held["backfill"], held["started"] = await _hold_migration(old)
        new = await initialize_services(*args, **kwargs)
        built.append(new)
        return new

    monkeypatch.setattr(_mcp_init, "initialize_services", building_while_a_migration_starts)

    with pytest.raises(VaultMigrationInFlightError) as excinfo:
        await registry_service.reload_vault("test_vault")

    _assert_refusal(excinfo.value.code, excinfo.value.detail, "test_vault", held["started"])
    await _assert_untouched(old, vaults, "test_vault")
    assert len(built) == 1
    # The replacement was closed rather than leaked: its store refuses dispatch.
    with pytest.raises(Exception):
        await built[0].graph_store.list_all_documents()
    await _release(held["task"], held["backfill"])


async def test_service_update_config_refused_before_writing(registry, tmp_vault_dir, config_writes):
    """Rival: refusing only at the reload, after the config has been written."""
    from sage.models.schemas import UpdateVaultConfigRequest

    _, vaults = registry
    old = vaults["test_vault"]
    task, backfill, started = await _hold_migration(old)
    body = UpdateVaultConfigRequest(vault=_vault_section(tmp_vault_dir, RENAMED))

    with pytest.raises(VaultMigrationInFlightError) as excinfo:
        await old.vault_config_service.update_config("test_vault", body, force=False)

    _assert_refusal(excinfo.value.code, excinfo.value.detail, "test_vault", started)
    assert config_writes == []
    await _assert_untouched(old, vaults, "test_vault")

    await _release(task, backfill)
    result = await old.vault_config_service.update_config("test_vault", body, force=False)
    assert result.status == "updated"
    assert config_writes == ["test_vault"]
    assert vaults["test_vault"].config.vault.name == RENAMED


async def test_service_update_config_dry_run_admitted_mid_migration(
    registry, tmp_vault_dir, config_writes
):
    """Rival: refusing a preview, which writes nothing and reloads nothing."""
    from sage.models.schemas import UpdateVaultConfigRequest

    _, vaults = registry
    old = vaults["test_vault"]
    task, backfill, _ = await _hold_migration(old)
    body = UpdateVaultConfigRequest(vault=_vault_section(tmp_vault_dir, RENAMED), dry_run=True)

    result = await old.vault_config_service.update_config("test_vault", body, force=False)

    assert result.status == "previewed"
    assert config_writes == []
    assert vaults["test_vault"] is old
    await _release(task, backfill)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest.fixture
async def app(minimal_vault_config_dict, tmp_vault_dir):
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(app, config)
    yield app
    try:
        await drain_vaults(app.state.vault_registry)
    finally:
        for services in app.state.vault_registry.values():
            services.close_timing()
            await services.close_storage()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_rest_reload_refused_mid_migration_then_admitted(app, client):
    vaults = app.state.vault_registry
    old = vaults["test_vault"]
    task, backfill, started = await _hold_migration(old)

    resp = await client.post("/sage_vaults/test_vault/maintenance/reload")

    assert resp.status_code == 409, resp.text
    body = resp.json()
    _assert_refusal(body["code"], body["detail"], "test_vault", started)
    await _assert_untouched(old, vaults, "test_vault")

    await _release(task, backfill)
    resp = await client.post("/sage_vaults/test_vault/maintenance/reload")
    assert resp.status_code == 200, resp.text
    assert vaults["test_vault"] is not old


async def test_rest_update_config_refused_mid_migration_then_admitted(
    app, client, tmp_vault_dir, config_writes
):
    vaults = app.state.vault_registry
    old = vaults["test_vault"]
    task, backfill, started = await _hold_migration(old)
    payload = {"vault": _vault_section(tmp_vault_dir, RENAMED)}

    resp = await client.put("/sage_vaults/test_vault/config", json=payload)

    assert resp.status_code == 409, resp.text
    body = resp.json()
    _assert_refusal(body["code"], body["detail"], "test_vault", started)
    assert config_writes == []
    await _assert_untouched(old, vaults, "test_vault")

    await _release(task, backfill)
    resp = await client.put("/sage_vaults/test_vault/config", json=payload)
    assert resp.status_code == 200, resp.text
    assert vaults["test_vault"].config.vault.name == RENAMED


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


@pytest.fixture
async def mcp_vault(minimal_vault_config_dict, tmp_vault_dir, monkeypatch):
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    registry_service = VaultRegistryService(_mcp._vaults, initialize_services)
    async with initialize_services_for_test(
        config,
        registry_service=registry_service,
        content_store_factory=lambda _brain: StubContentStore(),
    ) as services:
        _mcp._vaults["test_vault"] = services
        try:
            yield services
        finally:
            current = _mcp._vaults.get("test_vault")
            if current is not None and current is not services:
                try:
                    await drain_abstraction_queue(current.ingestion_service)
                finally:
                    current.close_timing()
                    await current.close_storage()
            _mcp._vaults.pop("test_vault", None)


async def test_mcp_reload_refused_mid_migration_then_admitted(mcp_vault):
    old = mcp_vault
    task, backfill, started = await _hold_migration(old)

    result = await mcp_reload_vault("test_vault")

    _assert_refusal(result["error"], result["detail"], "test_vault", started)
    await _assert_untouched(old, _mcp._vaults, "test_vault")

    await _release(task, backfill)
    result = await mcp_reload_vault("test_vault")
    assert result["reloaded"] is True
    assert _mcp._vaults["test_vault"] is not old


async def test_mcp_update_config_refused_mid_migration_then_admitted(
    mcp_vault, tmp_vault_dir, config_writes
):
    old = mcp_vault
    task, backfill, started = await _hold_migration(old)

    result = await mcp_update_vault_config(
        "test_vault", vault=_vault_section(tmp_vault_dir, RENAMED)
    )

    _assert_refusal(result["error"], result["detail"], "test_vault", started)
    assert config_writes == []
    await _assert_untouched(old, _mcp._vaults, "test_vault")

    await _release(task, backfill)
    result = await mcp_update_vault_config(
        "test_vault", vault=_vault_section(tmp_vault_dir, RENAMED)
    )
    assert result["status"] == "updated"
    assert _mcp._vaults["test_vault"].config.vault.name == RENAMED
