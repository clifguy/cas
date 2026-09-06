"""Closure-pair conformance for ``initialize_services`` call sites.

This module gates the closure-pair invariant declared in
``sage/mcp_init.py`` as ``REQUIRED_TRANSPORT_KWARGS``: every
transport-reachable production call site of ``initialize_services`` threads
each kwarg listed in the canonical set. Four call sites are surveyed:

  Transport lifespans / one-shot entrypoints
  ------------------------------------------
  - ``sage/mcp_server.py:reload_vault`` -- MCP reload tool
  - ``sage/app.py:_initialize_vault`` -- FastAPI lifespan

  Feature-operation call sites (reachable via FastAPI routers + MCP tools)
  -----------------------------------------------------------------------
  - ``sage/mcp_init.py:reload_vault_in_registry``
      (FastAPI PUT-config-reload + VaultRegistryService.reload)
  - ``sage/services/vault_registry.py:VaultRegistryService.create_vault``
      (FastAPI POST-create-vault + MCP create_vault)

Pattern: same shape as ``tests/sage/test_router_conformance.py``.
One ``TRANSPORT_SURFACES`` tuple declares the surface; one parametrized
test walks it. Closure-pair pattern per the *CAS Projection-Point Audit
Conventions*, applied to a non-projection-point surface.

Exclusion: ``sage/app.py:_initialize_services`` (the test-only backcompat
helper, ~10 callers across ``tests/app/`` and ``tests/sage/``) is
intentionally NOT in the surface. It already silently omits ``config_path``
because its callers pass pre-loaded ``VaultConfig`` instances and never
exercise on-disk reload. Treating it as conformant would require either
weakening the contract or rewriting every fixture; treating it as
non-conformant would flag a deliberate test-helper omission as a bug. The
helper is a documented test-bypass path, not a transport-reachable
production caller, and is excluded on that basis.

This file is **additive** to ``tests/sage/test_mcp_server_lifespan.py``;
the F13 tests there assert the stricter MCP-specific identity invariant
(``registry_service is mcp_server._vault_registry_service``). The
conformance test here asserts the weaker, parallel-surface key-presence
invariant -- silent omission fails; explicit ``None`` with a rationale
comment passes.
"""

from __future__ import annotations

import copy
import inspect
from pathlib import Path
from typing import Any, Awaitable, Callable, NamedTuple

import pytest
import yaml

import sage.app as sage_app
import sage.mcp_init as sage_mcp_init
import sage.mcp_server as mcp_server
from sage.config import VaultConfig, load_vault_config
from sage.mcp_init import REQUIRED_TRANSPORT_KWARGS, initialize_services
from sage.models.schemas import CreateVaultRequest
from sage.services.vault_registry import VaultRegistryService

# ---------------------------------------------------------------------------
# Helpers (local copies of the minimum machinery from
# test_mcp_server_lifespan.py; per the plan, fixtures stay co-located with
# their primary test file and minimal helpers are duplicated here rather
# than prematurely extracted).
# ---------------------------------------------------------------------------


def _materialize_vault(root: Path, vault_id: str, base_config: dict) -> Path:
    """Write a vault directory under ``root`` and return its config path."""
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
    async def close(self) -> None:
        pass

    async def list_all_documents(self) -> list:
        return []

    async def get_total_document_count(self) -> int:
        return 0


class _FakeUserService:
    async def bootstrap_owner(self) -> None:
        pass


class _FakeIngestionService:
    """Satisfies VaultRegistryService._build_vault_summary's iteration over
    ``services.ingestion_service.registered_adapters``, plus the abstraction-
    queue hooks the FastAPI lifespan and reload path call."""

    registered_adapters: dict = {}

    async def recover_incomplete_documents(self) -> int:
        return 0

    async def stop_worker(self) -> None:
        pass


class _FakeServices:
    """Minimal SAGEServices stand-in covering every attribute touched by
    the six drivers below: graph_store (close + list), config_path /
    content_store_factory / graph_store_factory / timing_thread
    (reload_vault_in_registry's close-and-reuse branch), close_timing and
    close_storage (every teardown path calls them), user_service.bootstrap_owner
    (VaultRegistryService.create_vault), ingestion_service (the same method's
    _build_vault_summary)."""

    def __init__(self, config: VaultConfig) -> None:
        self.config = config
        self.graph_store = _FakeGraphStore()
        self.user_service = _FakeUserService()
        self.ingestion_service = _FakeIngestionService()
        self.timing_thread = None
        self.config_path: Path | None = None
        self.content_store_factory: Any = None
        self.graph_store_factory: Any = None
        self.storage: Any = None

    def close_timing(self) -> None:
        # reload_vault_in_registry, the migrate CLI, and the FastAPI lifespan
        # call services.close_timing() on teardown; the fake owns no timing
        # thread/handler, so this is a no-op.
        pass

    async def close_storage(self) -> None:
        # Every teardown path calls services.close_storage(); mirror the
        # real implementation (graph store closed, then the storage handle's
        # resource released — the fake owns no handle).
        await self.graph_store.close()


@pytest.fixture
def isolate_module_state():
    """Save and restore mcp_server module state mutated by drivers."""
    saved_vaults = dict(mcp_server._vaults)
    yield
    mcp_server._vaults.clear()
    mcp_server._vaults.update(saved_vaults)


# ---------------------------------------------------------------------------
# Transport surface declaration
# ---------------------------------------------------------------------------

# A driver runs one call site's entrypoint under a monkeypatched
# ``initialize_services`` stub and returns the list of captured kwargs
# dicts. Drivers take a uniform set of fixtures so the parametrized test
# can dispatch over them.
_Driver = Callable[..., Awaitable[list[dict]]]


class TransportSurface(NamedTuple):
    label: str
    module_path: str
    driver: _Driver


async def _drive_mcp_reload(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    minimal_vault_config_dict: dict,
) -> list[dict]:
    """Drive sage.mcp_server.reload_vault.

    Sage_reload_vault now delegates to
    ``sage.mcp_init.reload_vault_in_registry``, which calls
    ``initialize_services`` from its own module namespace and builds the
    stack abstraction provider locally. The driver therefore patches both
    in ``sage.mcp_init`` (the current call site), not in
    ``sage.mcp_server``.
    """
    vault_root = tmp_path / "vault_root"
    vault_root.mkdir()
    config_path = _materialize_vault(vault_root, "vault_a", minimal_vault_config_dict)
    mcp_server._vaults.clear()

    config = load_vault_config(config_path)
    old_services = _FakeServices(config)
    old_services.config_path = config_path
    mcp_server._vaults["vault_a"] = old_services

    captured: list[dict] = []

    async def capturing_init(config, **kwargs):
        captured.append(kwargs)
        return _FakeServices(config)

    # Post-delegation, the live call to initialize_services and
    # build_stack_abstraction_provider happens inside reload_vault_in_registry
    # (sage.mcp_init), not at the import binding in sage.mcp_server.
    monkeypatch.setattr("sage.mcp_init.initialize_services", capturing_init)
    monkeypatch.setattr("sage.mcp_init.build_stack_abstraction_provider", lambda _cfg: object())

    await mcp_server.reload_vault("vault_a")
    return captured


async def _drive_fastapi_lifespan(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    minimal_vault_config_dict: dict,
) -> list[dict]:
    """Drive sage.app._initialize_vault (the FastAPI lifespan's per-vault step)."""
    from fastapi import FastAPI

    vault_root = tmp_path / "vault_root"
    vault_root.mkdir()
    config_path = _materialize_vault(vault_root, "vault_a", minimal_vault_config_dict)
    config = load_vault_config(config_path)

    # _ensure_registry_service inside _initialize_vault aliases
    # mcp_server._vaults onto app.state.vault_registry; clear so the
    # test starts from a known state. The isolate_module_state fixture
    # restores afterwards.
    mcp_server._vaults.clear()

    captured: list[dict] = []

    async def capturing_init(config, **kwargs):
        captured.append(kwargs)
        return _FakeServices(config)

    monkeypatch.setattr("sage.app.initialize_services", capturing_init)

    app = FastAPI()
    await sage_app._initialize_vault(app, config, config_path=config_path)
    return captured


async def _drive_reload_vault_in_registry(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    minimal_vault_config_dict: dict,
) -> list[dict]:
    """Drive sage.mcp_init.reload_vault_in_registry with a seeded registry
    so the close-and-reuse branch executes. This is the call site reached
    via VaultRegistryService.reload (FastAPI PUT-config-reload endpoint +
    MCP equivalent)."""
    vault_root = tmp_path / "vault_root"
    vault_root.mkdir()
    config_path = _materialize_vault(vault_root, "vault_a", minimal_vault_config_dict)
    config = load_vault_config(config_path)

    # Seed the registry so reload_vault_in_registry's close-and-reuse
    # branch runs (exercises the realistic FastAPI reload path).
    old = _FakeServices(config)
    old.config_path = config_path
    registry: dict = {"vault_a": old}

    captured: list[dict] = []

    async def capturing_init(config, **kwargs):
        captured.append(kwargs)
        return _FakeServices(config)

    monkeypatch.setattr("sage.mcp_init.initialize_services", capturing_init)

    await sage_mcp_init.reload_vault_in_registry(
        registry, "vault_a", config, config_path=config_path
    )
    return captured


async def _drive_vault_registry_create_vault(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    minimal_vault_config_dict: dict,
) -> list[dict]:
    """Drive sage.services.vault_registry.VaultRegistryService.create_vault.
    Reachable via the FastAPI POST-create-vault endpoint and the MCP
    create_vault tool."""
    # Redirect _VAULTS_ROOT so the config-yaml write create_vault now routes
    # through the vault-source store (CAS-ADR-043, default_vault_root() falls
    # back to _VAULTS_ROOT) lands inside tmp_path rather than polluting
    # ~/sage_vaults.
    vaults_dir = tmp_path / "sage_vaults"
    vaults_dir.mkdir()
    monkeypatch.setattr("sage.vault_management._VAULTS_ROOT", vaults_dir)

    # Build a minimal valid config dict for a vault whose id does not yet
    # exist in the registry.
    cfg = copy.deepcopy(minimal_vault_config_dict)
    cfg["vault"]["id"] = "vault_a"
    cfg["vault"]["name"] = "Vault A"
    cfg["vault"]["storage_root"] = str(tmp_path / "vault_a_sources")
    cfg["vault"]["brain_root"] = str(tmp_path / "vault_a_brain")

    captured: list[dict] = []

    async def capturing_init(config, **kwargs):
        captured.append(kwargs)
        return _FakeServices(config)

    registry: dict = {}
    vrs = VaultRegistryService(registry=registry, initialize_services=capturing_init)
    body = CreateVaultRequest(config=cfg)

    await vrs.create_vault(body)
    return captured


TRANSPORT_SURFACES: tuple[TransportSurface, ...] = (
    TransportSurface(
        label="mcp_reload_vault",
        module_path="sage.mcp_server",
        driver=_drive_mcp_reload,
    ),
    TransportSurface(
        label="fastapi_lifespan",
        module_path="sage.app",
        driver=_drive_fastapi_lifespan,
    ),
    TransportSurface(
        label="reload_vault_in_registry",
        module_path="sage.mcp_init",
        driver=_drive_reload_vault_in_registry,
    ),
    TransportSurface(
        label="vault_registry_create_vault",
        module_path="sage.services.vault_registry",
        driver=_drive_vault_registry_create_vault,
    ),
)


# ---------------------------------------------------------------------------
# T1: canonical-declaration shape
# ---------------------------------------------------------------------------


def test_required_transport_kwargs_constant_is_well_formed():
    """``REQUIRED_TRANSPORT_KWARGS`` must be a non-empty frozenset of strings
    naming keyword-only parameters on ``initialize_services``."""
    assert isinstance(REQUIRED_TRANSPORT_KWARGS, frozenset), (
        "REQUIRED_TRANSPORT_KWARGS must be a frozenset (immutable canonical declaration)"
    )
    assert len(REQUIRED_TRANSPORT_KWARGS) >= 2, (
        "REQUIRED_TRANSPORT_KWARGS must be non-empty; emptying it disables "
        "the conformance gate and lets the next F13-class regression ship"
    )
    assert all(isinstance(k, str) for k in REQUIRED_TRANSPORT_KWARGS)

    sig = inspect.signature(initialize_services)
    keyword_only = {
        name
        for name, param in sig.parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }
    drift = REQUIRED_TRANSPORT_KWARGS - keyword_only
    assert not drift, (
        f"REQUIRED_TRANSPORT_KWARGS names {drift!r}, which are not "
        "keyword-only parameters of initialize_services. Either the "
        "signature changed and the constant must follow, or the constant "
        "has a typo."
    )


# ---------------------------------------------------------------------------
# T2: parallel-surface key presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", TRANSPORT_SURFACES, ids=lambda s: s.label)
async def test_transport_threads_required_kwargs(
    surface: TransportSurface,
    isolate_module_state,
    monkeypatch,
    tmp_path,
    minimal_vault_config_dict,
):
    """Every transport-reachable production call site of
    ``initialize_services`` must thread every key listed in
    ``REQUIRED_TRANSPORT_KWARGS``. Presence is the contract; explicit
    ``None`` satisfies it.
    """
    captured = await surface.driver(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        minimal_vault_config_dict=minimal_vault_config_dict,
    )
    assert len(captured) >= 1, (
        f"{surface.label}: driver returned no captured calls -- the "
        "call site did not invoke initialize_services. Fix the driver, "
        "not the production code."
    )
    for call_kwargs in captured:
        missing = REQUIRED_TRANSPORT_KWARGS - call_kwargs.keys()
        assert not missing, (
            f"{surface.label} (module {surface.module_path}): "
            f"initialize_services called without required kwargs {sorted(missing)!r}. "
            "Every transport-reachable production call site must thread "
            "every key listed in sage.mcp_init.REQUIRED_TRANSPORT_KWARGS "
            "(T-0136 closure pair)."
        )


# ---------------------------------------------------------------------------
# T3: surface completeness
# ---------------------------------------------------------------------------


def test_transport_surface_is_complete():
    """The conformance surface must cover every transport-reachable
    production call site of ``initialize_services``. Adding a new call
    site (e.g., a future ROOT Harness orchestration entrypoint, or a new
    feature operation that constructs services for a vault) requires
    extending TRANSPORT_SURFACES *and* updating this test's expected
    label set -- deliberately. A silent drop would let a regression slip
    through unnoticed.
    """
    expected_labels = {
        "mcp_reload_vault",
        "fastapi_lifespan",
        "reload_vault_in_registry",
        "vault_registry_create_vault",
    }
    actual_labels = {s.label for s in TRANSPORT_SURFACES}
    assert actual_labels == expected_labels, (
        f"TRANSPORT_SURFACES drifted from the T-0136 documented set. "
        f"Missing: {expected_labels - actual_labels!r}, "
        f"Extra: {actual_labels - expected_labels!r}."
    )
