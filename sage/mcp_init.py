"""Shared SAGE service initialization for FastAPI and MCP entry points."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sage.adapters.content_store_lancedb import LanceDBContentStore
from sage.adapters.embedding_nomic import NomicEmbeddingProvider
from sage.adapters.interfaces import (
    AbstractionProvider,
    ContentStore,
    EmbeddingProvider,
)
from sage.adapters.stubs import StubAbstractionProvider
from sage.config import VaultConfig
from sage.models.enums import SourceType
from sage.services.documents import DocumentsService
from sage.services.graph_ops import GraphOpsService
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.metadata import MetadataService
from sage.services.retrieval import RetrievalService
from sage.services.staging_edges import StagingEdgesService
from sage.services.user_service import UserService
from sage.services.utilities import UtilitiesService
from sage.services.vault_config import VaultConfigService
from sage.source_adapters.docx_adapter import DocxAdapter
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.source_adapters.pdf_adapter import PdfAdapter
from sage.source_adapters.xlsx_adapter import XlsxAdapter
from sage.storage.graph_store import GraphStore
from sage.storage.locks import DocumentLockManager

if TYPE_CHECKING:
    from sage.services.vault_registry import VaultRegistryService


@dataclass
class SAGEServices:
    """All initialized SAGE services for a single vault."""

    config: VaultConfig
    graph_store: GraphStore
    content_store: ContentStore
    lock_manager: DocumentLockManager
    user_service: UserService
    lifecycle_service: LifecycleService
    metadata_service: MetadataService
    documents_service: DocumentsService
    ingestion_service: IngestionService
    graph_ops_service: GraphOpsService
    retrieval_service: RetrievalService
    utilities_service: UtilitiesService
    staging_edges_service: StagingEdgesService
    vault_config_service: VaultConfigService
    config_path: Path | None = None
    # Test-only hook: when set, reload paths (sage_reload_vault,
    # reload_vault_in_registry) re-invoke this factory with the vault's
    # brain_root instead of constructing a LanceDBContentStore. Carried on
    # the services tuple so the factory survives across reloads without
    # adding module-level mutable state. None in production.
    content_store_factory: Callable[[Path], ContentStore] | None = None


async def initialize_services(
    config: VaultConfig,
    *,
    content_store: ContentStore | None = None,
    content_store_factory: Callable[[Path], ContentStore] | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    abstraction_provider: AbstractionProvider | None = None,
    migrate: bool = False,
    config_path: Path | None = None,
    registry_service: "VaultRegistryService | None" = None,
) -> SAGEServices:
    """Initialize all SAGE services for a vault configuration.

    Args:
        config: Loaded and validated vault configuration.
        content_store: Optional override (default: LanceDBContentStore).
        content_store_factory: Optional callable invoked with the vault's
            ``brain_root`` to build a ContentStore. Used by hermetic
            lifespan tests to substitute ``StubContentStore`` without
            mutating module state. Ignored if ``content_store`` is also
            passed; stored on the returned ``SAGEServices`` so reload
            paths reuse the same factory.
        embedding_provider: Optional override (default: NomicEmbeddingProvider).
        abstraction_provider: Optional override (default: from config).
        migrate: If True, apply any pending schema migrations to the graph
            store and content store. If False (default), raise
            ``SchemaMigrationRequired`` when a migration is needed.
        config_path: Source path of the vault_config.yaml file. Stored on
            the returned ``SAGEServices`` so that ``sage_reload_vault`` can
            re-read the file from disk to pick up edits made externally.
        registry_service: Singleton VaultRegistryService used by
            VaultConfigService.update_config to perform the registry-mutation
            step of a config reload. Optional in test fixtures that never
            mutate the registry; required in normal app startup.

    Returns:
        SAGEServices dataclass with all services ready to use.
    """
    brain_root = Path(config.vault.brain_root).expanduser()
    brain_root.mkdir(parents=True, exist_ok=True)

    graph_store = GraphStore(brain_root / "graph.db")
    await graph_store.initialize(migrate=migrate)

    lock_manager = DocumentLockManager()

    # Content store: explicit instance > factory > production LanceDB.
    if content_store is None:
        if content_store_factory is not None:
            content_store = content_store_factory(brain_root)
        else:
            content_store = LanceDBContentStore(brain_root, migrate=migrate)

    # Embedding provider: injected or production Nomic. CI sets
    # SAGE_TEST_STUB_PROVIDERS=1 so the ~700 tests that construct
    # services via this path don't each load the ~270MB nomic model
    # into a 7 GB Linux runner (T-0018). Tests that exercise the real
    # adapter (@requires_embedding in test_adapters.py) construct
    # NomicEmbeddingProvider directly and are unaffected.
    if embedding_provider is None:
        if os.environ.get("SAGE_TEST_STUB_PROVIDERS") == "1":
            from sage.adapters.stubs import StubEmbeddingProvider

            embedding_provider = StubEmbeddingProvider()
        else:
            embedding_provider = NomicEmbeddingProvider()

    # Abstraction provider: injected, or Qwen3/stub from config.
    # SAGE_TEST_STUB_PROVIDERS=1 forces the stub regardless of config so that
    # tests cannot accidentally load Qwen3 (~16GB MLX/Metal) alongside the
    # running MCP server. The env var was originally added for nomic (T-0018)
    # and is extended to abstraction here as the interim guardrail for T-0029
    # against the kernel-panic-class failure documented in F-8.
    if abstraction_provider is None:
        if os.environ.get("SAGE_TEST_STUB_PROVIDERS") == "1" or not (
            config.abstraction.enabled and config.abstraction.model
        ):
            abstraction_provider = StubAbstractionProvider()
        else:
            from sage.adapters.abstraction_qwen3 import Qwen3AbstractionProvider

            abstraction_provider = Qwen3AbstractionProvider(
                model_id=config.abstraction.model,
            )

    # Source adapters
    source_adapters = {
        SourceType.MARKDOWN: MarkdownAdapter(),
        SourceType.DOCX: DocxAdapter(),
        SourceType.XLSX: XlsxAdapter(),
        SourceType.PDF: PdfAdapter(),
    }

    # Services
    user_service = UserService(graph_store, config)
    lifecycle_service = LifecycleService(graph_store, lock_manager, config)
    metadata_service = MetadataService(graph_store, lock_manager, config, content_store)
    documents_service = DocumentsService(graph_store, config)
    ingestion_service = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=content_store,
        embedding_provider=embedding_provider,
        abstraction_provider=abstraction_provider,
        config=config,
        source_adapters=source_adapters,
        lifecycle_service=lifecycle_service,
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
    staging_edges_service = StagingEdgesService(graph_store)
    vault_config_service = VaultConfigService(graph_store, content_store, config, registry_service)

    # Bootstrap vault owner
    await user_service.bootstrap_owner()

    return SAGEServices(
        config=config,
        graph_store=graph_store,
        content_store=content_store,
        lock_manager=lock_manager,
        user_service=user_service,
        lifecycle_service=lifecycle_service,
        metadata_service=metadata_service,
        documents_service=documents_service,
        ingestion_service=ingestion_service,
        graph_ops_service=graph_ops_service,
        retrieval_service=retrieval_service,
        utilities_service=utilities_service,
        staging_edges_service=staging_edges_service,
        vault_config_service=vault_config_service,
        config_path=config_path,
        content_store_factory=content_store_factory,
    )


async def reload_vault_in_registry(
    registry: dict[str, SAGEServices],
    vault_id: str,
    config: VaultConfig,
    config_path: Path | None = None,
    registry_service: "VaultRegistryService | None" = None,
) -> SAGEServices:
    """Close old services for a vault and reinitialize from a new config.

    Used by the PUT config endpoint after writing updated YAML.
    Parallels the MCP server's sage_reload_vault tool. Carries any
    ``content_store_factory`` from the predecessor services forward so
    hermetic-lifespan-test setups survive reload.
    """
    old = registry.get(vault_id)
    content_store_factory = None
    if old:
        await old.graph_store.close()
        if config_path is None:
            config_path = old.config_path
        content_store_factory = old.content_store_factory
    new_services = await initialize_services(
        config,
        config_path=config_path,
        registry_service=registry_service,
        content_store_factory=content_store_factory,
    )
    registry[vault_id] = new_services
    return new_services
