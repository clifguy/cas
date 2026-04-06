"""FastAPI dependency injection for vault scoping and service access."""

from fastapi import Depends, Path, Request

from sage.api.errors import VaultNotFoundError
from sage.config import VaultConfig
from sage.services.graph_ops import GraphOpsService
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.metadata import MetadataService
from sage.services.retrieval import RetrievalService
from sage.services.user_service import UserService
from sage.services.utilities import UtilitiesService
from sage.storage.graph_store import GraphStore
from sage.storage.locks import DocumentLockManager


async def get_vault_id(
    vault_id: str = Path(..., description="Vault identifier"),
    request: Request = None,
) -> str:
    """Validate vault_id against loaded config."""
    config: VaultConfig = request.app.state.config
    if vault_id != config.vault.id:
        raise VaultNotFoundError(vault_id)
    return vault_id


async def get_graph_store(request: Request) -> GraphStore:
    return request.app.state.graph_store


async def get_lock_manager(request: Request) -> DocumentLockManager:
    return request.app.state.lock_manager


async def get_lifecycle_service(request: Request) -> LifecycleService:
    return request.app.state.lifecycle_service


async def get_metadata_service(request: Request) -> MetadataService:
    return request.app.state.metadata_service


async def get_user_service(request: Request) -> UserService:
    return request.app.state.user_service


async def get_ingestion_service(request: Request) -> IngestionService:
    return request.app.state.ingestion_service


async def get_graph_ops_service(request: Request) -> GraphOpsService:
    return request.app.state.graph_ops_service


async def get_retrieval_service(request: Request) -> RetrievalService:
    return request.app.state.retrieval_service


async def get_utilities_service(request: Request) -> UtilitiesService:
    return request.app.state.utilities_service


async def get_config(request: Request) -> VaultConfig:
    return request.app.state.config
