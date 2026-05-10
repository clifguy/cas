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
    _config_path_for_vault,
    _validate_config,
    _write_config_yaml,
)

if TYPE_CHECKING:
    from sage.mcp_init import SAGEServices


class VaultRegistryService:
    def __init__(
        self,
        registry: dict[str, "SAGEServices"],
        initialize_services: Callable[..., Awaitable["SAGEServices"]],
    ) -> None:
        self._registry = registry
        self._initialize_services = initialize_services

    async def list_vaults(self) -> list[VaultSummary]:
        """Return all vaults registered with the running SAGE instance."""
        results: list[VaultSummary] = []
        for _vault_id, services in self._registry.items():
            cfg = services.config
            project_counts = await services.graph_store.get_document_counts_by_field("project")
            projects = sorted(project_counts.keys())
            results.append(self._build_vault_summary(cfg, services, projects))
        return results

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

        config_path = _config_path_for_vault(vault_id)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        Path(config.vault.storage_root).expanduser().mkdir(parents=True, exist_ok=True)
        Path(config.vault.brain_root).expanduser().mkdir(parents=True, exist_ok=True)

        _write_config_yaml(config_path, body.config)

        services = await self._initialize_services(config, registry_service=self)
        self._registry[vault_id] = services

        await services.user_service.bootstrap_owner()

        return self._build_vault_summary(config, services)

    async def reload(
        self, vault_id: str, new_config: VaultConfig
    ) -> "SAGEServices":
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
        """Build a VaultSummary from a config and services instance."""
        vault = config.vault
        doc_types = [
            VaultDocTypeEntry(value=dt.value, label=dt.label)
            for dt in config.document_types.doc_types
        ]
        lifecycle_states = [
            VaultLifecycleState(
                value=s.value, label=s.label, is_terminal=s.is_terminal
            )
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
