"""Cross-vault registry operations: list, create, reload.

Owns the work behind the cross-vault routes (GET /sage_vaults,
POST /sage_vaults) and the registry-mutation step of vault reload
(invoked by VaultConfigService.update_config after a successful YAML
write).

This service is a singleton on app.state, not a per-vault service. The
registry dict it holds is the same one aliased in sage/app.py:lifespan
from sage.mcp_server._vaults so REST and MCP transports share the same
SAGEServices bundles.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from sage.api.errors import VaultAlreadyExistsError
from sage.config import VaultConfig
from sage.models.schemas import (
    CreateVaultRequest,
    VaultAdapterInfo,
    VaultDocTypeEntry,
    VaultLifecycleState,
    VaultSummary,
)
from sage.vault_management import (
    _atomic_write_bytes,
    _validate_config,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sage.mcp_init import SAGEServices


_VAULTS_ROOT = Path("~/sage_vaults").expanduser()


class VaultRegistryService:
    def __init__(
        self,
        registry: dict[str, "SAGEServices"],
        initialize_services: Callable[..., Awaitable["SAGEServices"]],
    ) -> None:
        self._registry = registry
        self._initialize_services = initialize_services

    @staticmethod
    def get_default_config(vault_id: str, name: str, owner: str) -> dict:
        """Build a minimal valid default config dict for a new vault.

        Callers that want to spin up a vault with sensible defaults pass
        the result of this method as the ``config`` argument to
        ``create_vault`` (or to the REST create-vault endpoint).
        """
        return {
            "vault": {
                "id": vault_id,
                "name": name,
                "owner": owner,
                "storage_root": str(_VAULTS_ROOT / vault_id / "sources"),
                "brain_root": str(_VAULTS_ROOT / vault_id / "brain"),
                "visibility": "personal",
            },
            "document_types": {
                "doc_types": [
                    {
                        "value": "document",
                        "label": "Document",
                        "description": "General-purpose document type.",
                    },
                    {
                        "value": "reference",
                        "label": "Reference",
                        "description": "Reference material and supporting documents.",
                    },
                ],
            },
            "lifecycle": {
                "base_states_required": True,
                "states": [
                    {"value": "active", "label": "Active"},
                    {"value": "completed", "label": "Completed"},
                    {"value": "archived", "label": "Archived", "is_terminal": True},
                ],
                "transitions": [
                    {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                    {
                        "from_state": "active",
                        "action": "supersede",
                        "to_state": "archived",
                        "creates_edge": "supersedes",
                    },
                    {"from_state": "active", "action": "complete", "to_state": "completed"},
                    {"from_state": "active", "action": "archive", "to_state": "archived"},
                    {"from_state": "completed", "action": "archive", "to_state": "archived"},
                    {"from_state": "archived", "action": "reactivate", "to_state": "active"},
                ],
            },
            "source_adapters": {
                "adapters": [
                    {"source_type": "markdown", "enabled": True},
                    {"source_type": "docx", "enabled": True},
                    {"source_type": "xlsx", "enabled": True},
                    {"source_type": "pdf", "enabled": True},
                ],
            },
            "metadata_extraction": {
                "filename_extraction": {
                    "separator": "_",
                },
            },
            "edge_inference": {
                "tier_assignments": [
                    {
                        "edge_type": "supersedes",
                        "tier": 1,
                        "inference_rules": [{"method": "version_chain"}],
                    },
                ],
            },
            "abstraction": {"enabled": False},
        }

    async def list_vaults(self) -> list[VaultSummary]:
        """Return all vaults registered with the running SAGE instance.

        Resilient per vault: a vault is skipped from the listing (and logged)
        when its backing store errors OR when the storage-presence probe says
        its durable backing is gone -- one dead vault never fails the whole
        listing. Mirrors the log-and-drop discipline of the startup discovery
        loop. The explicit probe matters because an out-of-band teardown does
        not reliably error: the per-vault search_path can resolve the store's
        unqualified table names against a later entry, so a torn-down vault's
        queries may keep succeeding against tables that are not its own.

        Self-healing: when the skipped vault is also gone from vault-source
        discovery (a completed teardown removes both the schema and the config),
        it is evicted from the live registry so the stale entry does not linger
        until the next restart. A skipped vault whose config is still present is
        left in place -- a transient error (or a half-completed teardown, which
        a restart repairs by re-bootstrapping from the surviving config) must
        not destroy the registry entry.
        """
        results: list[VaultSummary] = []
        # Computed once, lazily, only if a vault is skipped -- discovery can be
        # a remote round-trip under the document-store binding.
        discovered_ids: set[str] | None = None
        # Snapshot: an eviction below mutates self._registry mid-iteration.
        for vault_id, services in list(self._registry.items()):
            try:
                if await services.graph_store.storage_present(vault_id):
                    project_counts = await services.graph_store.get_document_counts_by_field(
                        "project"
                    )
                    projects = sorted(project_counts.keys())
                    results.append(self._build_vault_summary(services.config, services, projects))
                    continue
                logger.error("Skipping vault %s from the listing: durable backing absent", vault_id)
            except Exception:
                logger.exception(
                    "Skipping vault %s from the listing: backing store error", vault_id
                )
            # Best-effort reconcile -- must never re-break the listing.
            try:
                if discovered_ids is None:
                    discovered_ids = self._discovered_vault_ids()
                if vault_id not in discovered_ids:
                    await self._evict(vault_id)
                    logger.warning(
                        "Evicted vault %s from the registry: no longer present in "
                        "vault-source discovery",
                        vault_id,
                    )
            except Exception:
                logger.exception("Registry reconcile for vault %s failed; left in place", vault_id)
        return results

    def _discovered_vault_ids(self) -> set[str]:
        """Ids of the vaults the active profile's vault-source store currently holds.

        Resolves the store the same way ``create_vault`` does (CAS-ADR-043).
        ``discover()`` -- not ``config_locator`` -- is the profile-invariant
        existence signal: the document-store binding returns ``None`` from
        ``config_locator`` by design, so a config-locator check would wrongly
        report every cloud vault as gone.
        """
        from sage.mcp_init import (
            get_stack_config,
            get_vault_root,
            resolve_stack_vault_source_store,
        )

        store = resolve_stack_vault_source_store(get_stack_config(), vault_root=get_vault_root())
        return {d.vault_id for d in store.discover() if d.vault_id}

    async def _evict(self, vault_id: str) -> None:
        """Drop a vault from the live registry and release its resources.

        Same teardown order the reload path uses -- stop the ingestion worker,
        close the timing flusher, release the per-vault storage pool. Pops
        first so a concurrent lister that also hit the error path gets ``None``
        and does not double-close.
        """
        services = self._registry.pop(vault_id, None)
        if services is None:
            return
        await services.ingestion_service.stop_worker()
        services.close_timing()
        await services.close_storage()

    async def create_vault(self, body: CreateVaultRequest) -> VaultSummary:
        """Create a new vault from a full config dict.

        Validates the config, creates the vault directories, writes
        vault_config.yaml, initializes services, registers the vault, and
        bootstraps the owner.
        """
        config = _validate_config(body.config)
        vault_id = config.vault.id

        if vault_id in self._registry:
            raise VaultAlreadyExistsError(vault_id)

        # CAS-ADR-042/043: thread the active deployment profile's abstraction
        # binding through (local profile -> the stack-built provider,
        # CAS-ADR-030), and persist the configuration declaration through the
        # profile's vault-source store so provisioning is an act against the
        # store rather than a hard-coded filesystem write.
        from sage.mcp_init import (
            get_stack_config,
            get_vault_root,
            resolve_stack_abstraction_provider,
            resolve_stack_vault_source_store,
        )

        stack_config = get_stack_config()
        stack_provider = resolve_stack_abstraction_provider(stack_config)
        vault_source_store = resolve_stack_vault_source_store(
            stack_config, vault_root=get_vault_root()
        )

        config_path = vault_source_store.config_locator(vault_id)
        Path(config.vault.storage_root).expanduser().mkdir(parents=True, exist_ok=True)
        Path(config.vault.brain_root).expanduser().mkdir(parents=True, exist_ok=True)

        # Snapshot the pre-write yaml bytes (almost always None, since this
        # path runs only for a vault_id absent from the registry) so a
        # failure below can restore the on-disk state without leaving an
        # orphan yaml in place.
        old_yaml_bytes = (
            config_path.read_bytes() if config_path is not None and config_path.exists() else None
        )
        vault_source_store.write_config(vault_id, body.config)

        try:
            services = await self._initialize_services(
                config,
                config_path=config_path,
                registry_service=self,
                abstraction_provider=stack_provider,
            )
            self._registry[vault_id] = services
            await services.user_service.bootstrap_owner()
        except BaseException:
            # Roll back the registry entry and any services that were
            # constructed before the failure point, so the user can retry
            # create_vault cleanly. The original exception propagates;
            # rollback failures log and are swallowed.
            registered = self._registry.pop(vault_id, None)
            if registered is not None:
                try:
                    registered.close_timing()
                except BaseException:
                    logger.exception(
                        "timing teardown failed during create_vault rollback for vault_id=%s",
                        vault_id,
                    )
                try:
                    await registered.close_storage()
                except BaseException:
                    logger.exception(
                        "graph_store close failed during create_vault rollback for vault_id=%s",
                        vault_id,
                    )
            try:
                if old_yaml_bytes is None:
                    vault_source_store.delete_config(vault_id)
                elif config_path is not None:
                    _atomic_write_bytes(config_path, old_yaml_bytes)
            except BaseException:
                logger.exception(
                    "rollback of vault_config.yaml failed after create_vault "
                    "failure for vault_id=%s; orphan yaml may exist on disk",
                    vault_id,
                )
            raise

        return self._build_vault_summary(config, services)

    async def reload(self, vault_id: str, new_config: VaultConfig) -> "SAGEServices":
        """Close old services for a vault and reinitialize from a new config.

        Delegates to mcp_init.reload_vault_in_registry; the registry-service
        wrapper threads the registry dict and the back-reference to self so
        the new VaultConfigService is wired correctly after the reload.
        """
        from sage.mcp_init import reload_vault_in_registry

        return await reload_vault_in_registry(
            self._registry, vault_id, new_config, registry_service=self
        )

    @staticmethod
    def _build_vault_summary(
        config: VaultConfig,
        services: Any,
        projects: list[str] | None = None,
    ) -> VaultSummary:
        """Build a VaultSummary from a config and services instance.

        Single owner of the ``VaultConfig`` -> ``VaultSummary`` projection per
        the *CAS Projection-Point Audit Conventions* steering document
        (cas vault, doc_type=steering_document). The exhaustive-fields test
        ``test_build_vault_summary_populates_every_vault_summary_field`` in
        ``tests/sage/test_vault_registry.py`` fails closed if a field is
        added to ``VaultSummary`` (or to its sub-models ``VaultDocTypeEntry``,
        ``VaultLifecycleState``, ``VaultAdapterInfo``) but not wired through
        this factory; the test exercises the sub-models transitively on the
        first element of each sub-collection rather than splitting closure
        pairs onto each sub-model individually.
        """
        vault = config.vault
        doc_types = [
            VaultDocTypeEntry(value=dt.value, label=dt.label)
            for dt in config.document_types.doc_types
        ]
        lifecycle_states = [
            VaultLifecycleState(value=s.value, label=s.label, is_terminal=s.is_terminal)
            for s in config.lifecycle.states
        ]
        adapters = [
            VaultAdapterInfo(
                source_type=source_type.value,
                enabled=True,
                extensions=adapter.EXTENSIONS,
            )
            for source_type, adapter in services.ingestion_service.registered_adapters.items()
        ]
        return VaultSummary(
            id=vault.id,
            name=vault.name,
            description=getattr(vault, "description", None),
            storage_root=vault.storage_root,
            doc_types=doc_types,
            lifecycle_states=lifecycle_states,
            adapters=adapters,
            projects=projects or [],
        )
