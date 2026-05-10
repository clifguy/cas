"""FastAPI dependency injection for vault scoping and service access.

Multi-vault: all service dependencies resolve via the vault registry
keyed by vault_id. The get_vault_id dependency validates the vault_id
and makes it available downstream. Service dependencies use it to
look up the correct SAGEServices instance.
"""

from fastapi import Depends, Path, Request

from sage.api.errors import VaultNotFoundError
from sage.config import VaultConfig
from sage.mcp_init import SAGEServices
from sage.services.documents import DocumentsService
from sage.services.graph_ops import GraphOpsService
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.metadata import MetadataService
from sage.services.retrieval import RetrievalService
from sage.services.staging_edges import StagingEdgesService
from sage.services.user_service import UserService
from sage.services.utilities import UtilitiesService
from sage.storage.graph_store import GraphStore
from sage.storage.locks import DocumentLockManager


def _get_services(request: Request, vault_id: str) -> SAGEServices:
    """Look up SAGEServices for a validated vault_id."""
    registry: dict[str, SAGEServices] = request.app.state.vault_registry
    if vault_id not in registry:
        raise VaultNotFoundError(vault_id)
    return registry[vault_id]


async def get_vault_id(
    vault_id: str = Path(..., description="Vault identifier"),
    request: Request = None,
) -> str:
    """Validate vault_id against loaded vault registry."""
    registry: dict[str, SAGEServices] = request.app.state.vault_registry
    if vault_id not in registry:
        raise VaultNotFoundError(vault_id)
    return vault_id


async def get_graph_store(
    request: Request,
    vault_id: str = Depends(get_vault_id),
) -> GraphStore:
    return _get_services(request, vault_id).graph_store


async def get_lock_manager(
    request: Request,
    vault_id: str = Depends(get_vault_id),
) -> DocumentLockManager:
    return _get_services(request, vault_id).lock_manager


async def get_lifecycle_service(
    request: Request,
    vault_id: str = Depends(get_vault_id),
) -> LifecycleService:
    return _get_services(request, vault_id).lifecycle_service


async def get_metadata_service(
    request: Request,
    vault_id: str = Depends(get_vault_id),
) -> MetadataService:
    return _get_services(request, vault_id).metadata_service


async def get_documents_service(
    request: Request,
    vault_id: str = Depends(get_vault_id),
) -> DocumentsService:
    return _get_services(request, vault_id).documents_service


async def get_staging_edges_service(
    request: Request,
    vault_id: str = Depends(get_vault_id),
) -> StagingEdgesService:
    return _get_services(request, vault_id).staging_edges_service


async def get_user_service(
    request: Request,
    vault_id: str = Depends(get_vault_id),
) -> UserService:
    return _get_services(request, vault_id).user_service


async def get_ingestion_service(
    request: Request,
    vault_id: str = Depends(get_vault_id),
) -> IngestionService:
    return _get_services(request, vault_id).ingestion_service


async def get_graph_ops_service(
    request: Request,
    vault_id: str = Depends(get_vault_id),
) -> GraphOpsService:
    return _get_services(request, vault_id).graph_ops_service


async def get_retrieval_service(
    request: Request,
    vault_id: str = Depends(get_vault_id),
) -> RetrievalService:
    return _get_services(request, vault_id).retrieval_service


async def get_utilities_service(
    request: Request,
    vault_id: str = Depends(get_vault_id),
) -> UtilitiesService:
    return _get_services(request, vault_id).utilities_service


async def get_config(
    request: Request,
    vault_id: str = Depends(get_vault_id),
) -> VaultConfig:
    return _get_services(request, vault_id).config
