"""Shared SAGE service initialization for FastAPI and MCP entry points."""

from dataclasses import dataclass
from pathlib import Path

from sage.adapters.stubs import StubAbstractionProvider, StubContentStore, StubEmbeddingProvider
from sage.config import VaultConfig, load_vault_config
from sage.models.enums import SourceType
from sage.services.graph_ops import GraphOpsService
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.metadata import MetadataService
from sage.services.retrieval import RetrievalService
from sage.services.user_service import UserService
from sage.services.utilities import UtilitiesService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.storage.graph_store import GraphStore
from sage.storage.locks import DocumentLockManager


@dataclass
class SAGEServices:
    """All initialized SAGE services for a single vault."""

    config: VaultConfig
    graph_store: GraphStore
    lock_manager: DocumentLockManager
    user_service: UserService
    lifecycle_service: LifecycleService
    metadata_service: MetadataService
    ingestion_service: IngestionService
    graph_ops_service: GraphOpsService
    retrieval_service: RetrievalService
    utilities_service: UtilitiesService


async def initialize_services(config: VaultConfig) -> SAGEServices:
    """Initialize all SAGE services for a vault configuration.

    Args:
        config: Loaded and validated vault configuration.

    Returns:
        SAGEServices dataclass with all services ready to use.
    """
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
    graph_ops_service = GraphOpsService(graph_store, config)
    retrieval_service = RetrievalService(
        graph_store=graph_store,
        content_store=content_store,
        embedding_provider=embedding_provider,
        config=config,
    )
    utilities_service = UtilitiesService(
        graph_store=graph_store,
        content_store=content_store,
        embedding_provider=embedding_provider,
        config=config,
    )

    # Bootstrap vault owner
    await user_service.bootstrap_owner()

    return SAGEServices(
        config=config,
        graph_store=graph_store,
        lock_manager=lock_manager,
        user_service=user_service,
        lifecycle_service=lifecycle_service,
        metadata_service=metadata_service,
        ingestion_service=ingestion_service,
        graph_ops_service=graph_ops_service,
        retrieval_service=retrieval_service,
        utilities_service=utilities_service,
    )
