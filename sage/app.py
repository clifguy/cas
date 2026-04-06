"""FastAPI application factory for the SAGE Core API."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from sage.api.errors import register_exception_handlers
from sage.api.routers import documents, graph_ops, ingestion, lifecycle, metadata, retrieval, users, utilities
from sage.config import VaultConfig, load_vault_config
from sage.mcp_init import initialize_services


async def _initialize_services(app: FastAPI, config: VaultConfig) -> None:
    """Initialize all services and store them in app.state."""
    services = await initialize_services(config)

    app.state.config = services.config
    app.state.graph_store = services.graph_store
    app.state.lock_manager = services.lock_manager
    app.state.user_service = services.user_service
    app.state.lifecycle_service = services.lifecycle_service
    app.state.metadata_service = services.metadata_service
    app.state.ingestion_service = services.ingestion_service
    app.state.graph_ops_service = services.graph_ops_service
    app.state.retrieval_service = services.retrieval_service
    app.state.utilities_service = services.utilities_service


def create_app(config_path: Path | None = None, config: VaultConfig | None = None) -> FastAPI:
    """Create and configure the SAGE Core API application.

    Args:
        config_path: Path to vault YAML config file.
        config: Pre-loaded VaultConfig (used in testing).
            Exactly one of config_path or config must be provided.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if config_path is not None:
            vault_config = load_vault_config(config_path)
        elif config is not None:
            vault_config = config
        else:
            raise ValueError("Either config_path or config must be provided")

        await _initialize_services(app, vault_config)
        yield
        await app.state.graph_store.close()

    app = FastAPI(
        title="SAGE Core API",
        version="0.1.0",
        description="Salience-Aware Graph Engine - Core API",
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    app.include_router(ingestion.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(documents.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(lifecycle.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(metadata.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(users.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(graph_ops.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(retrieval.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(utilities.router, prefix="/sage_vaults/{vault_id}")

    return app
