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
    _validate_config,
    _write_config_yaml,
    config_path_for_vault,
)

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
        ``sage_create_vault`` (or to the REST create-vault endpoint).
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

        config_path = config_path_for_vault(vault_id)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        Path(config.vault.storage_root).expanduser().mkdir(parents=True, exist_ok=True)
        Path(config.vault.brain_root).expanduser().mkdir(parents=True, exist_ok=True)

        _write_config_yaml(config_path, body.config)

        # CAS-ADR-030: thread the stack-built abstraction provider through.
        from sage.mcp_init import build_stack_abstraction_provider, get_stack_config

        stack_provider = build_stack_abstraction_provider(get_stack_config())
        services = await self._initialize_services(
            config,
            config_path=config_path,
            registry_service=self,
            abstraction_provider=stack_provider,
        )
        self._registry[vault_id] = services

        await services.user_service.bootstrap_owner()

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
        """Build a VaultSummary from a config and services instance (T-0122).

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
