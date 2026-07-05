"""Vault-config write paths honor the process-bound vault root.

Regression coverage for the asymmetry where ``create_vault`` and
``update_config`` resolved the vault-source binding with the *default* root
(``~/sage_vaults``) while discovery honored the lifespan-resolved root
(``--vault-root`` / ``SAGE_VAULT_ROOT``). A process on a non-default root wrote
new ``vault_config.yaml`` declarations outside its own view, invisible to itself
and silently inherited by the operator's default-root deployment at its next
restart.

Each test publishes the bound root through ``mcp_init.set_vault_root`` (exactly
what the transport lifespans do) and points ``SAGE_VAULT_ROOT`` at a *distinct*
scratch directory, so ``default_vault_root()`` differs from the bound root: the
"config lands under the bound root and nowhere else" assertion is the regression
guard — with the pre-fix code the config lands under the default root instead.
The distinct-root discipline also keeps every write inside ``tmp_path``, never
the real vault tree.
"""

from __future__ import annotations

import pytest
import yaml

from sage import mcp_init
from sage.config import SageCoreConfig, VaultConfig
from sage.models.schemas import CreateVaultRequest, UpdateVaultConfigRequest
from sage.services.vault_config import VaultConfigService
from sage.services.vault_registry import VaultRegistryService
from sage.vault_source_binding import DocumentStoreVaultSourceStore, FilesystemVaultSourceStore

# ---------------------------------------------------------------------------
# Fakes: minimal surface the write paths touch, so no storage is provisioned.
# ---------------------------------------------------------------------------


class _FakeUserService:
    async def bootstrap_owner(self) -> None:  # create_vault calls this post-register
        pass


class _FakeIngestionService:
    # _build_vault_summary iterates registered_adapters.items().
    registered_adapters: dict = {}


class _FakeServices:
    def __init__(self) -> None:
        self.user_service = _FakeUserService()
        self.ingestion_service = _FakeIngestionService()


class _FakeRegistryService:
    async def reload(self, vault_id: str, new_config: VaultConfig) -> None:
        # update_config delegates the registry-mutation step here; a no-op keeps
        # the test off initialize_services (and therefore off Postgres).
        return None


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """A published *bound* root and a distinct *default* root.

    ``SAGE_VAULT_ROOT`` names the default so ``default_vault_root()`` (the
    pre-fix write target) resolves to it; the bound root is published as a
    lifespan would. A stub-provider env keeps ``create_vault``'s abstraction
    resolution off any real model, and the stack config is pinned to the
    filesystem-backed local default.
    """
    bound = tmp_path / "bound_root"
    bound.mkdir()
    default = tmp_path / "default_root"
    default.mkdir()
    monkeypatch.setenv("SAGE_VAULT_ROOT", str(default))
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    monkeypatch.delenv("SAGE_TEST_VAULT_SOURCE_BACKEND", raising=False)
    monkeypatch.setattr(mcp_init, "_stack_config", SageCoreConfig())
    monkeypatch.setattr(mcp_init, "_vault_root", bound)
    return bound, default


# ---------------------------------------------------------------------------
# Seam accessor
# ---------------------------------------------------------------------------


async def test_get_set_vault_root_roundtrip(tmp_path, monkeypatch):
    """set_vault_root publishes the root get_vault_root returns; None clears it."""
    monkeypatch.setattr(mcp_init, "_vault_root", None)
    assert mcp_init.get_vault_root() is None

    root = tmp_path / "some_root"
    mcp_init.set_vault_root(root)
    assert mcp_init.get_vault_root() == root

    mcp_init.set_vault_root(None)
    assert mcp_init.get_vault_root() is None


# ---------------------------------------------------------------------------
# create_vault write path
# ---------------------------------------------------------------------------


async def test_create_vault_writes_config_under_bound_root(roots):
    """create_vault persists vault_config.yaml under the published bound root,
    never under the default root.

    Trap (anti-coincidental): the pre-fix call site omits ``vault_root=`` and
    resolves the filesystem binding at ``default_vault_root()`` (the distinct
    default root here), so the config lands there. The not-under-default
    assertion is what fails on the pre-fix code.
    """
    bound, default = roots

    async def fake_init(config, **kwargs):
        return _FakeServices()

    registry: dict = {}
    svc = VaultRegistryService(registry=registry, initialize_services=fake_init)

    config = VaultRegistryService.get_default_config("bound_vault", "Bound Vault", "owner")
    # Keep storage/brain under the bound root too, so create_vault's mkdirs stay
    # inside tmp_path; only the config *path* is governed by the resolved root.
    config["vault"]["storage_root"] = str(bound / "bound_vault" / "sources")
    config["vault"]["brain_root"] = str(bound / "bound_vault" / "brain")

    await svc.create_vault(CreateVaultRequest(config=config))

    assert (bound / "bound_vault" / "vault_config.yaml").exists()
    assert not (default / "bound_vault" / "vault_config.yaml").exists()
    assert "bound_vault" in registry


# ---------------------------------------------------------------------------
# update_config write path
# ---------------------------------------------------------------------------


async def test_update_config_writes_under_bound_root(roots):
    """update_config persists the merged vault_config.yaml under the bound root,
    never under the default root.

    Trap (anti-coincidental): same missing ``vault_root=`` on the update call
    site; the merged config would land under the default root. The change is
    non-destructive (only vault.name), so _check_destructive_changes never
    queries the (unused) graph store — keeping the test off Postgres.
    """
    bound, default = roots
    vault_id = "upd_vault"

    config_dict = VaultRegistryService.get_default_config(vault_id, "Original", "owner")
    config_dict["vault"]["storage_root"] = str(bound / vault_id / "sources")
    config_dict["vault"]["brain_root"] = str(bound / vault_id / "brain")

    # Seed the existing declaration under the bound root via the production writer.
    FilesystemVaultSourceStore(bound).write_config(vault_id, config_dict)
    assert (bound / vault_id / "vault_config.yaml").exists()

    loaded = VaultConfig.model_validate(config_dict)
    svc = VaultConfigService(
        graph_store=object(),  # never touched by a non-destructive update
        content_store=object(),
        config=loaded,
        registry_service=_FakeRegistryService(),
    )

    new_vault_section = dict(config_dict["vault"])
    new_vault_section["name"] = "Renamed"
    body = UpdateVaultConfigRequest(vault=new_vault_section, dry_run=False)

    await svc.update_config(vault_id, body, force=False)

    written = yaml.safe_load((bound / vault_id / "vault_config.yaml").read_text())
    assert written["vault"]["name"] == "Renamed"
    assert not (default / vault_id / "vault_config.yaml").exists()


# ---------------------------------------------------------------------------
# Cloud profile: the document-store binding ignores the vault root
# ---------------------------------------------------------------------------


def test_document_store_backend_ignores_vault_root(tmp_path, monkeypatch):
    """A document-store-backed profile resolves the document-store binding
    whether or not a root is published — threading the root cannot force a
    filesystem write on the cloud profile.

    The env override selects the document-store backend with a lazy client, so
    no Azure/managed-identity resolution occurs.
    """
    monkeypatch.setenv("SAGE_TEST_VAULT_SOURCE_BACKEND", "document_store")
    cfg = SageCoreConfig()

    via_none = mcp_init.resolve_stack_vault_source_store(cfg, vault_root=None)
    via_path = mcp_init.resolve_stack_vault_source_store(cfg, vault_root=tmp_path)

    assert isinstance(via_none, DocumentStoreVaultSourceStore)
    assert isinstance(via_path, DocumentStoreVaultSourceStore)
