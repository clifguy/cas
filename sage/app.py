"""FastAPI application factory for the SAGE Core API."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from sage.adapters.stubs import StubAbstractionProvider, StubContentStore, StubEmbeddingProvider
from sage.api.errors import register_exception_handlers
from sage.api.routers import documents, graph_ops, ingestion, lifecycle, metadata, users
from sage.config import VaultConfig, load_vault_config
from sage.models.enums import SourceType
from sage.services.graph_ops import GraphOpsService
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.metadata import MetadataService
from sage.services.user_service import UserService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.storage.graph_store import GraphStore
from sage.storage.locks import DocumentLockManager


async def _initialize_services(app: FastAPI, config: VaultConfig) -> None:
    """Initialize all services and store them in app.state."""
    brain_root = Path(config.vault.brain_root).expanduser()
    brain_root.mkdir(parents=True, exist_ok=True)

    graph_store = GraphStore(brain_root / "graph.db")
    await graph_store.initialize()

    lock_manager = DocumentLockManager()

    # Stub adapters for Phase 1 (swap for real implementations later)
    content_store = StubContentStore()
    embedding_provider = StubEmbeddingProvider()
    abstraction_provider = StubAbstractionProvider()

    # Source adapters
    source_adapters = {
        SourceType.MARKDOWN: MarkdownAdapter(),
    }

    # Services
    user_service = UserService(graph_store, config)
    lifecycle_service = LifecycleService(graph_store, lock_manager, config)
    metadata_service = MetadataService(graph_store, lock_manager, config)
    ingestion_service = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=content_store,
        embedding_provider=embedding_provider,
        abstraction_provider=abstraction_provider,
        config=config,
        source_adapters=source_adapters,
    )

    # Bootstrap vault owner (BH-009)
    await user_service.bootstrap_owner()

    # Store in app.state for dependency injection
    app.state.config = config
    app.state.graph_store = graph_store
    app.state.lock_manager = lock_manager
    app.state.user_service = user_service
    app.state.lifecycle_service = lifecycle_service
    app.state.metadata_service = metadata_service
    app.state.ingestion_service = ingestion_service

    graph_ops_service = GraphOpsService(graph_store, config)
    app.state.graph_ops_service = graph_ops_service


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

    return app
