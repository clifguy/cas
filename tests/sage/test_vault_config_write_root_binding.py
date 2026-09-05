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

The same root authority governs the *vault directory* callers derive from
``config_path_for_vault``, not just the config declaration itself: the
maintenance audit log (``.maintenance_log.jsonl``) is written by
``MaintenanceService`` and read back by ``VaultConfigService.get_stats``, so
writer and reader must land on the same directory under a redirected root. The
fixture therefore publishes three *distinct* roots -- bound, ``SAGE_VAULT_ROOT``,
and the module-level literal -- so an assertion can tell apart a correct
resolution, a resolution that honors only the environment variable, and one that
reaches the literal directly (CAS-ADR-043).
"""

from __future__ import annotations

from datetime import datetime

import pytest
import yaml

from sage import mcp_init, vault_management
from sage.adapters.stubs import StubContentStore, StubGraphStore
from sage.config import SageCoreConfig, VaultConfig
from sage.models.schemas import CreateVaultRequest, Document, UpdateVaultConfigRequest
from sage.services.maintenance import MaintenanceService
from sage.services.maintenance_log import MAINTENANCE_LOG_FILENAME
from sage.services.vault_config import VaultConfigService
from sage.services.vault_registry import VaultRegistryService
from sage.vault_management import config_path_for_vault
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


class _FakeCreatedGraphStore:
    """The one read ``create_vault`` makes of a new vault's store: its document count."""

    async def get_total_document_count(self) -> int:
        return 0


class _FakeServices:
    def __init__(self) -> None:
        self.graph_store = _FakeCreatedGraphStore()
        self.user_service = _FakeUserService()
        self.ingestion_service = _FakeIngestionService()


class _FakeRegistryService:
    async def reload(self, vault_id: str, new_config: VaultConfig) -> None:
        # update_config delegates the registry-mutation step here; a no-op keeps
        # the test off initialize_services (and therefore off Postgres).
        return None


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Three mutually distinct roots: *bound*, *default*, and *literal*.

    ``SAGE_VAULT_ROOT`` names the default so ``default_vault_root()`` (the
    pre-fix write target) resolves to it; the bound root is published as a
    lifespan would; the module-level literal is redirected to a third directory
    so it is distinguishable from both. Three roots rather than two because two
    cannot separate a resolution that honors the environment variable from one
    that honors the bound root -- the distinction this module exists to pin.

    A stub-provider env keeps ``create_vault``'s abstraction resolution off any
    real model, and the stack config is pinned to the filesystem-backed local
    default.
    """
    bound = tmp_path / "bound_root"
    bound.mkdir()
    default = tmp_path / "default_root"
    default.mkdir()
    literal = tmp_path / "literal_root"
    literal.mkdir()
    monkeypatch.setenv("SAGE_VAULT_ROOT", str(default))
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    monkeypatch.delenv("SAGE_TEST_VAULT_SOURCE_BACKEND", raising=False)
    monkeypatch.setattr(vault_management, "_VAULTS_ROOT", literal)
    monkeypatch.setattr(mcp_init, "_stack_config", SageCoreConfig())
    monkeypatch.setattr(mcp_init, "_vault_root", bound)
    return bound, default, literal


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
    bound, default, _literal = roots

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
    bound, default, _literal = roots
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

    Asserted against the resolved store's binding rather than the store itself:
    the resolver returns it wrapped in the source-byte refusal translation
    (CAS-ADR-043), and the claim here is about which backend dispatch chose,
    which the wrapper does not participate in.
    """
    monkeypatch.setenv("SAGE_TEST_VAULT_SOURCE_BACKEND", "document_store")
    cfg = SageCoreConfig()

    via_none = mcp_init.resolve_stack_vault_source_store(cfg, vault_root=None)
    via_path = mcp_init.resolve_stack_vault_source_store(cfg, vault_root=tmp_path)

    assert isinstance(via_none.binding, DocumentStoreVaultSourceStore)
    assert isinstance(via_path.binding, DocumentStoreVaultSourceStore)


# ---------------------------------------------------------------------------
# config_path_for_vault: the shared root-resolution chain
# ---------------------------------------------------------------------------


def test_config_path_resolves_under_bound_root(roots):
    """config_path_for_vault resolves against the published bound root.

    Trap (anti-coincidental): the three roots are distinct directories, so a
    resolution that reaches the module-level literal (the pre-fix body) lands
    under ``literal`` and a resolution that honors only ``SAGE_VAULT_ROOT``
    lands under ``default``. Both negative assertions fail those two shapes;
    only the full chain satisfies all three.
    """
    bound, default, literal = roots

    path = config_path_for_vault("v")

    assert path == bound / "v" / "vault_config.yaml"
    assert not path.is_relative_to(default)
    assert not path.is_relative_to(literal)


def test_config_path_falls_back_to_default_root_when_unbound(roots, monkeypatch):
    """With no bound root published, resolution falls through to
    ``default_vault_root()`` and honors ``SAGE_VAULT_ROOT``.

    Trap (anti-coincidental): an implementation that consults only the bound
    root returns nothing usable here -- the fallback leg is what this pins.
    """
    _bound, default, literal = roots
    monkeypatch.setattr(mcp_init, "_vault_root", None)

    path = config_path_for_vault("v")

    assert path == default / "v" / "vault_config.yaml"
    assert not path.is_relative_to(literal)


def test_config_path_falls_back_to_literal_when_unbound_and_unset(roots, monkeypatch):
    """With neither a bound root nor ``SAGE_VAULT_ROOT``, resolution reaches the
    module-level literal.

    This leg is what keeps the suite-wide root redirection effective: the
    autouse fixture that steers writes away from the real vault tree patches
    that literal, so dropping the leg would send every test's config write to
    the operator's own vaults.
    """
    _bound, default, literal = roots
    monkeypatch.setattr(mcp_init, "_vault_root", None)
    monkeypatch.delenv("SAGE_VAULT_ROOT")

    path = config_path_for_vault("v")

    assert path == literal / "v" / "vault_config.yaml"
    assert not path.is_relative_to(default)


# ---------------------------------------------------------------------------
# Derived vault directory: the maintenance audit log's writer/reader pair
# ---------------------------------------------------------------------------


class _EmptyStatsGraphStore(StubGraphStore):
    """Graph store answering the four aggregate reads ``get_stats`` makes that
    the shared stub deliberately refuses.

    The shared stub raises on these rather than returning an empty result, so a
    test that only needs the surrounding call to complete has to say so
    explicitly. Nothing here is under assertion: this module pins *where*
    ``get_stats`` looks for the maintenance log, not the aggregates it reports.
    """

    async def get_document_counts_by_field(self, field: str) -> dict[str, int]:
        return {}

    async def get_last_ingestion_at(self) -> datetime | None:
        return None

    async def count_documents_by_pipeline_status(self, status: str) -> int:
        return 0

    async def list_pending_metadata_documents(self) -> list[Document]:
        return []


def _maintenance_without_vault_dir(config: VaultConfig) -> MaintenanceService:
    """MaintenanceService with no ``vault_dir`` override, so the audit-log path
    is derived from the shared root-resolution chain rather than injected."""
    return MaintenanceService(
        vault_id=config.vault.id,
        graph_store=StubGraphStore(),
        config=config,
        registry_service=None,
        content_store=StubContentStore(),
        vault_dir=None,
    )


async def test_maintenance_audit_log_lands_under_bound_root(roots, minimal_config):
    """The maintenance audit log is written under the bound root.

    Trap (anti-coincidental): ``vault_dir`` is explicitly None so the derived
    branch runs; with the pre-fix derivation the log lands under ``literal``,
    which the negative assertions reject.
    """
    bound, default, literal = roots
    vault_id = minimal_config.vault.id

    await _maintenance_without_vault_dir(minimal_config).optimize_content_store(
        cleanup_older_than_days=0
    )

    assert (bound / vault_id / MAINTENANCE_LOG_FILENAME).exists()
    assert not (default / vault_id / MAINTENANCE_LOG_FILENAME).exists()
    assert not (literal / vault_id / MAINTENANCE_LOG_FILENAME).exists()


async def test_get_stats_reads_optimize_log_from_bound_root(roots, minimal_config):
    """Writer and reader agree on the vault directory under a redirected root.

    ``MaintenanceService`` appends the audit record and
    ``VaultConfigService.get_stats`` reads it back; both derive the directory
    from the same chain, so this pins the *pairing*, not the target: it holds
    whenever the two agree, including when they agree on the wrong directory.
    The sibling tests above own the target -- they assert the absolute location
    of the config path and of the audit log. What this one catches is a change
    that moves one side of the pair and not the other, which leaves
    ``last_optimize`` None after a real optimize. The pre-assert rules out a
    stale log satisfying the post-assert by accident.
    """
    _bound, _default, _literal = roots
    maintenance = _maintenance_without_vault_dir(minimal_config)
    stats_service = VaultConfigService(
        _EmptyStatsGraphStore(), StubContentStore(), minimal_config, None
    )

    assert (await stats_service.get_stats()).last_optimize is None

    report = await maintenance.optimize_content_store(cleanup_older_than_days=0)

    last_optimize = (await stats_service.get_stats()).last_optimize
    assert last_optimize is not None
    assert last_optimize.bytes_reclaimed == report.bytes_reclaimed
