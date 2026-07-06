"""Tests for sage.mcp_server lifespan and reload behavior.

The MCP servers are mounted on the SAGE FastAPI app, whose lifespan owns
the vault registry; mcp_server's own lifespan must be a no-op so the
parent app's setup is not disturbed. The reload path threads the
module-scope registry service (F13 conformance).
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

import sage.mcp_server as mcp_server
from sage.config import VaultConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _materialize_vault(root: Path, vault_id: str, base_config: dict) -> Path:
    """Write a vault directory and return its config path."""
    vault_dir = root / vault_id
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / "sources").mkdir(exist_ok=True)
    (vault_dir / "brain").mkdir(exist_ok=True)

    cfg = copy.deepcopy(base_config)
    cfg["vault"]["id"] = vault_id
    cfg["vault"]["name"] = vault_id.replace("_", " ").title()
    cfg["vault"]["storage_root"] = str(vault_dir / "sources")
    cfg["vault"]["brain_root"] = str(vault_dir / "brain")
    config_path = vault_dir / "vault_config.yaml"
    config_path.write_text(yaml.safe_dump(cfg))
    return config_path


class _FakeGraphStore:
    def __init__(self) -> None:
        self.close_called = False

    async def close(self) -> None:
        self.close_called = True


class _FakeIngestionService:
    """Stub abstraction-queue hooks the reload path calls."""

    async def recover_incomplete_documents(self) -> int:
        return 0

    async def stop_worker(self) -> None:
        pass


class _FakeServices:
    def __init__(self, config: VaultConfig) -> None:
        self.config = config
        self.graph_store = _FakeGraphStore()
        self.ingestion_service = _FakeIngestionService()
        # reload_vault_in_registry calls close_timing() on each services.
        # None is the no-timing-thread sentinel; the no-op close_timing
        # below stands in for the real stop-thread-and-release-handler
        # teardown.
        self.timing_thread = None

    def close_timing(self) -> None:
        pass

    async def close_storage(self) -> None:
        # Every teardown path calls services.close_storage(); the fake
        # mirrors the real contract (graph store closed, no storage handle).
        await self.graph_store.close()


@pytest.fixture
def isolate_module_state():
    """Save and restore module-level state used by the lifespan."""
    saved_vaults = dict(mcp_server._vaults)

    yield

    mcp_server._vaults.clear()
    mcp_server._vaults.update(saved_vaults)


@pytest.fixture
def vault_root(tmp_path) -> Path:
    root = tmp_path / "vault_root"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# Surface 5
# ---------------------------------------------------------------------------


async def test_lifespan_does_not_touch_registry(isolate_module_state):
    """#23: The lifespan must leave _vaults alone.

    The FastAPI lifespan (sage/app.py) owns the vault lifecycle and
    pre-populates the shared registry; mcp_server's lifespan doing any
    init or teardown would fight it.
    """
    sentinel = object()
    mcp_server._vaults.clear()
    mcp_server._vaults["external_vault"] = sentinel

    async with mcp_server._lifespan(mcp_server.mcp):
        # Inside the lifespan: registry must be unchanged.
        assert mcp_server._vaults == {"external_vault": sentinel}

    # After teardown: still unchanged — teardown is the parent app's job.
    assert mcp_server._vaults == {"external_vault": sentinel}


# ---------------------------------------------------------------------------
# F13 conformance: every initialize_services call from this module must thread
# the module-scope _vault_registry_service. Without it, MaintenanceService is
# not constructed (sage/mcp_init.py:392) and the admin tools refuse the call.
# Discovered 2026-05-21; fix commit c0bf7ed.
# ---------------------------------------------------------------------------


async def test_sage_reload_vault_threads_registry_service_into_initialize_services(
    isolate_module_state,
    monkeypatch,
    vault_root,
    minimal_vault_config_dict,
):
    """F13 conformance for the reload path: reload_vault must pass
    registry_service=_vault_registry_service to initialize_services."""
    config_path = _materialize_vault(vault_root, "vault_a", minimal_vault_config_dict)
    mcp_server._vaults.clear()

    # Seed the registry with a fake services entry to satisfy the reload
    # preconditions (config_path + content_store_factory + graph_store_factory
    # + graph_store.close).
    from sage.config import load_vault_config

    config = load_vault_config(config_path)
    old_services = _FakeServices(config)
    old_services.config_path = config_path
    old_services.content_store_factory = None
    old_services.graph_store_factory = None
    mcp_server._vaults["vault_a"] = old_services

    captured: list[dict] = []

    class _CountingGraphStore(_FakeGraphStore):
        async def list_all_documents(self) -> list:
            return []

    class _CountingServices(_FakeServices):
        def __init__(self, config) -> None:  # noqa: ANN001
            super().__init__(config)
            self.graph_store = _CountingGraphStore()

    async def capturing_init(config, **kwargs):
        captured.append(kwargs)
        return _CountingServices(config)

    # Sage_reload_vault now delegates to reload_vault_in_registry
    # (in sage.mcp_init), which calls initialize_services and
    # build_stack_abstraction_provider from its own module namespace.
    monkeypatch.setattr("sage.mcp_init.initialize_services", capturing_init)
    # build_stack_abstraction_provider would otherwise construct a real
    # Qwen3 process; replace with a sentinel since the reload path only
    # forwards it through initialize_services (which is itself stubbed).
    monkeypatch.setattr(
        "sage.mcp_init.build_stack_abstraction_provider",
        lambda _cfg: object(),
    )

    await mcp_server.reload_vault("vault_a")

    assert len(captured) == 1
    assert captured[0].get("registry_service") is mcp_server._vault_registry_service, (
        "reload_vault must thread _vault_registry_service into "
        "initialize_services; without it, the post-reload services bundle has "
        "maintenance_service=None and the admin tools refuse the call (F13)"
    )
