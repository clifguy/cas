"""Tests for sage.mcp_server standalone lifespan behavior.

The MCP server runs in two modes:
  - **Mounted**: FastAPI's lifespan owns the vault registry; mcp_server's
    lifespan must be a no-op so the parent app's setup is not disturbed.
  - **Standalone**: invoked as ``python -m sage.mcp_server``, the lifespan
    discovers vaults from ``_vault_root`` and populates ``_vaults`` itself.

These tests exercise both modes by setting the module-level globals
directly. ``initialize_services`` is monkeypatched so tests do not pull
in heavy provider initialization.
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


def _materialize_vault(
    root: Path, vault_id: str, base_config: dict, malformed: bool = False
) -> Path:
    """Write a vault directory and return its config path."""
    vault_dir = root / vault_id
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / "sources").mkdir(exist_ok=True)
    (vault_dir / "brain").mkdir(exist_ok=True)

    config_path = vault_dir / "vault_config.yaml"
    if malformed:
        config_path.write_text("not: valid: yaml: ::: [unclosed")
        return config_path

    cfg = copy.deepcopy(base_config)
    cfg["vault"]["id"] = vault_id
    cfg["vault"]["name"] = vault_id.replace("_", " ").title()
    cfg["vault"]["storage_root"] = str(vault_dir / "sources")
    cfg["vault"]["brain_root"] = str(vault_dir / "brain")
    config_path.write_text(yaml.safe_dump(cfg))
    return config_path


class _FakeGraphStore:
    def __init__(self) -> None:
        self.close_called = False

    async def close(self) -> None:
        self.close_called = True


class _FakeIngestionService:
    """Stub abstraction-queue hooks the standalone lifespan + reload path call."""

    async def recover_incomplete_documents(self) -> int:
        return 0

    async def stop_worker(self) -> None:
        pass


class _FakeServices:
    def __init__(self, config: VaultConfig) -> None:
        self.config = config
        self.graph_store = _FakeGraphStore()
        self.ingestion_service = _FakeIngestionService()
        # reload_vault_in_registry and the standalone-lifespan teardown call
        # close_timing() on each services. None is the no-timing-thread
        # sentinel; the no-op close_timing below stands in for the real
        # stop-thread-and-release-handler teardown.
        self.timing_thread = None

    def close_timing(self) -> None:
        pass


@pytest.fixture
def isolate_module_state(monkeypatch):
    """Save and restore module-level state used by the lifespan."""
    saved_vaults = dict(mcp_server._vaults)
    saved_root = mcp_server._vault_root

    yield

    mcp_server._vaults.clear()
    mcp_server._vaults.update(saved_vaults)
    mcp_server._vault_root = saved_root


@pytest.fixture
def vault_root(tmp_path) -> Path:
    root = tmp_path / "vault_root"
    root.mkdir()
    return root


def _patch_initialize_services(monkeypatch):
    """Monkeypatch initialize_services to return a fake services object."""

    async def fake_init(config, **kwargs):
        return _FakeServices(config)

    monkeypatch.setattr("sage.mcp_server.initialize_services", fake_init)


# ---------------------------------------------------------------------------
# Surface 5
# ---------------------------------------------------------------------------


async def test_mounted_mode_does_not_touch_registry(isolate_module_state, monkeypatch):
    """#23: When _vault_root is None, the lifespan must leave _vaults alone."""
    sentinel = object()
    monkeypatch.setattr(mcp_server, "_vault_root", None)
    mcp_server._vaults.clear()
    mcp_server._vaults["external_vault"] = sentinel

    async with mcp_server._lifespan(mcp_server.mcp):
        # Inside the lifespan: registry must be unchanged.
        assert mcp_server._vaults == {"external_vault": sentinel}

    # After teardown: still unchanged. Standalone teardown would clear it,
    # but mounted mode must not.
    assert mcp_server._vaults == {"external_vault": sentinel}


async def test_standalone_mode_populates_registry_from_discovery(
    isolate_module_state,
    monkeypatch,
    vault_root,
    minimal_vault_config_dict,
):
    """#24: Standalone discovers vaults under _vault_root and registers each."""
    _materialize_vault(vault_root, "vault_a", minimal_vault_config_dict)
    _materialize_vault(vault_root, "vault_b", minimal_vault_config_dict)
    monkeypatch.setattr(mcp_server, "_vault_root", vault_root)
    mcp_server._vaults.clear()
    _patch_initialize_services(monkeypatch)

    async with mcp_server._lifespan(mcp_server.mcp):
        assert set(mcp_server._vaults.keys()) == {"vault_a", "vault_b"}


async def test_standalone_mode_isolates_bad_vault(
    isolate_module_state,
    monkeypatch,
    vault_root,
    minimal_vault_config_dict,
    caplog,
):
    """#25: A vault whose config fails to parse is logged and skipped."""
    _materialize_vault(vault_root, "good_vault", minimal_vault_config_dict)
    _materialize_vault(vault_root, "broken_vault", minimal_vault_config_dict, malformed=True)
    monkeypatch.setattr(mcp_server, "_vault_root", vault_root)
    mcp_server._vaults.clear()
    _patch_initialize_services(monkeypatch)

    with caplog.at_level("ERROR"):
        async with mcp_server._lifespan(mcp_server.mcp):
            assert "good_vault" in mcp_server._vaults
            assert "broken_vault" not in mcp_server._vaults

    assert any("broken_vault" in r.message or "broken" in r.message.lower() for r in caplog.records)


async def test_standalone_teardown_clears_registry(
    isolate_module_state,
    monkeypatch,
    vault_root,
    minimal_vault_config_dict,
):
    """#26: After exiting the standalone lifespan, _vaults is empty."""
    _materialize_vault(vault_root, "vault_a", minimal_vault_config_dict)
    monkeypatch.setattr(mcp_server, "_vault_root", vault_root)
    mcp_server._vaults.clear()
    _patch_initialize_services(monkeypatch)

    async with mcp_server._lifespan(mcp_server.mcp):
        assert "vault_a" in mcp_server._vaults

    assert mcp_server._vaults == {}


# ---------------------------------------------------------------------------
# F13 conformance: every initialize_services call from this module must thread
# the module-scope _vault_registry_service. Without it, MaintenanceService is
# not constructed (sage/mcp_init.py:392) and the admin tools refuse the call.
# Discovered 2026-05-21; fix commit c0bf7ed.
# ---------------------------------------------------------------------------


async def test_standalone_lifespan_threads_registry_service_into_initialize_services(
    isolate_module_state,
    monkeypatch,
    vault_root,
    minimal_vault_config_dict,
):
    """F13 conformance: standalone lifespan must pass registry_service=
    _vault_registry_service to every initialize_services call. Asserted for
    every discovered vault."""
    _materialize_vault(vault_root, "vault_a", minimal_vault_config_dict)
    _materialize_vault(vault_root, "vault_b", minimal_vault_config_dict)
    monkeypatch.setattr(mcp_server, "_vault_root", vault_root)
    mcp_server._vaults.clear()

    captured: list[dict] = []

    async def capturing_init(config, **kwargs):
        captured.append(kwargs)
        return _FakeServices(config)

    monkeypatch.setattr("sage.mcp_server.initialize_services", capturing_init)

    async with mcp_server._lifespan(mcp_server.mcp):
        pass

    assert len(captured) == 2
    for call_kwargs in captured:
        assert call_kwargs.get("registry_service") is mcp_server._vault_registry_service, (
            "lifespan must thread _vault_registry_service into initialize_services; "
            "without it, services.maintenance_service is None and the admin tools "
            "refuse the call (F13)"
        )


async def test_sage_reload_vault_threads_registry_service_into_initialize_services(
    isolate_module_state,
    monkeypatch,
    vault_root,
    minimal_vault_config_dict,
):
    """F13 conformance for the reload path: reload_vault must pass
    registry_service=_vault_registry_service to initialize_services."""
    config_path = _materialize_vault(vault_root, "vault_a", minimal_vault_config_dict)
    monkeypatch.setattr(mcp_server, "_vault_root", vault_root)
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
