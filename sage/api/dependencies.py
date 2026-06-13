"""FastAPI dependency injection for vault scoping and service access.

Multi-vault: all service dependencies resolve via the vault registry
keyed by vault_id. The get_vault_id dependency validates the vault_id
and makes it available downstream. Service dependencies use it to
look up the correct SAGEServices instance.
"""

from fastapi import Depends, Request

from sage.adapters.interfaces import GraphStore
from sage.api.errors import VaultNotFoundError
from sage.config import VaultConfig
from sage.mcp_init import SAGEServices
from sage.models.schemas import VaultIdStr
from sage.services.documents import DocumentsService
from sage.services.graph_ops import GraphOpsService
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.maintenance import MaintenanceService
from sage.services.metadata import MetadataService
from sage.services.retrieval import RetrievalService
from sage.services.staging_edges import StagingEdgesService
from sage.services.user_service import UserService
from sage.services.utilities import UtilitiesService
from sage.services.vault_config import VaultConfigService
from sage.services.vault_registry import VaultRegistryService
from sage.storage.locks import DocumentLockManager


def _get_services(request: Request, vault_id: str) -> SAGEServices:
    """Look up SAGEServices for a vault_id that has already passed shape and registry checks."""
    registry: dict[str, SAGEServices] = request.app.state.vault_registry
    if vault_id not in registry:
        raise VaultNotFoundError(vault_id)
    return registry[vault_id]


async def get_vault_id(
    vault_id: VaultIdStr,
    request: Request,
) -> VaultIdStr:
    """Validate vault_id against loaded vault registry.

    The ``VaultIdStr`` alias on the parameter enforces shape via Pydantic at
    request binding; FastAPI returns 422 on shape violations before this
    function runs. Registry membership is then checked here and raises
    ``VaultNotFoundError`` (404) on miss.

    The bare annotation (no ``Path(...)`` default) is deliberate: FastAPI
    strips the ``AfterValidator`` from an ``Annotated`` type when a
    ``Path(...)`` factory is also present as the default value. FastAPI
    still binds ``vault_id`` from the path parameter ``{vault_id}`` in
    the route pattern via name matching.
    """
    registry: dict[str, SAGEServices] = request.app.state.vault_registry
    if vault_id not in registry:
        raise VaultNotFoundError(vault_id)
    return vault_id


async def get_graph_store(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> GraphStore:
    return _get_services(request, vault_id).graph_store


async def get_lock_manager(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> DocumentLockManager:
    return _get_services(request, vault_id).lock_manager


async def get_lifecycle_service(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> LifecycleService:
    return _get_services(request, vault_id).lifecycle_service


async def get_metadata_service(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> MetadataService:
    return _get_services(request, vault_id).metadata_service


async def get_documents_service(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> DocumentsService:
    return _get_services(request, vault_id).documents_service


async def get_staging_edges_service(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> StagingEdgesService:
    return _get_services(request, vault_id).staging_edges_service


async def get_user_service(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> UserService:
    return _get_services(request, vault_id).user_service


async def get_ingestion_service(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> IngestionService:
    return _get_services(request, vault_id).ingestion_service


async def get_graph_ops_service(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> GraphOpsService:
    return _get_services(request, vault_id).graph_ops_service


async def get_vault_services(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> SAGEServices:
    """Return the full SAGEServices bundle for a vault.

    Used by multi-service orchestrations (e.g. the batch-ingest pipeline)
    that compose several services -- ingestion, graph store, graph ops,
    config -- in one operation rather than depending on a single
    per-service factory.
    """
    return _get_services(request, vault_id)


async def get_retrieval_service(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> RetrievalService:
    return _get_services(request, vault_id).retrieval_service


async def get_utilities_service(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> UtilitiesService:
    return _get_services(request, vault_id).utilities_service


async def get_config(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> VaultConfig:
    return _get_services(request, vault_id).config


async def get_vault_registry_service(request: Request) -> VaultRegistryService:
    return request.app.state.vault_registry_service


async def get_vault_config_service(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> VaultConfigService:
    return _get_services(request, vault_id).vault_config_service


async def get_maintenance_service(
    request: Request,
    vault_id: VaultIdStr = Depends(get_vault_id),
) -> MaintenanceService:
    services = _get_services(request, vault_id)
    if services.maintenance_service is None:
        raise RuntimeError(
            f"Vault {vault_id!r} was initialized without a registry_service; "
            "maintenance_service is unavailable. The production lifespan path "
            "always supplies one (CAS-ADR-029)."
        )
    return services.maintenance_service
