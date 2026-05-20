"""Shared SAGE service initialization for FastAPI and MCP entry points."""

from __future__ import annotations

import logging
import logging.handlers
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sage.adapters.content_store_lancedb import LanceDBContentStore
from sage.adapters.embedding_nomic import get_nomic_embedding_provider
from sage.adapters.interfaces import (
    AbstractionProvider,
    ContentStore,
    EmbeddingProvider,
)
from sage.adapters.stubs import StubAbstractionProvider
from sage.config import VaultConfig
from sage.instrumentation.timing import (
    NULL_QUERY_TIMER,
    NullQueryTimer,
    QueryTimer,
    TimingConfig,
    VaultTimingThread,
)
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

_TIMING_LOGGER_NAMES = (
    "sage.storage.timing",
    "sage.content.timing",
    "sage.retrieval.timing",
)


def _install_timing_handler(
    log_path: Path,
) -> logging.handlers.RotatingFileHandler:
    """Attach a per-vault rotating FileHandler to the three timing loggers.

    Idempotent: if a handler already points at ``log_path`` on a logger,
    it is reused. ``propagate`` is disabled on the timing loggers so
    records don't bubble up to the root logger (which would mix them
    into the normal app stream).
    """
    handler = logging.handlers.RotatingFileHandler(
        str(log_path),
        maxBytes=50_000_000,
        backupCount=3,
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    for name in _TIMING_LOGGER_NAMES:
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        existing = [
            h
            for h in logger.handlers
            if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == log_path
        ]
        if not existing:
            logger.addHandler(handler)
        logger.propagate = False
    return handler


def _build_vault_timers(
    timing: TimingConfig,
    brain_root: Path,
) -> tuple[
    QueryTimer | NullQueryTimer,
    QueryTimer | NullQueryTimer,
    QueryTimer | NullQueryTimer,
    VaultTimingThread | None,
    logging.handlers.RotatingFileHandler | None,
]:
    """Construct the three per-layer QueryTimers and the flusher thread.

    Returns ``(storage, content, retrieval, thread, handler)``. When
    ``timing.enabled`` is False, all three are ``NULL_QUERY_TIMER`` and
    the thread/handler are ``None``.
    """
    if not timing.enabled:
        return NULL_QUERY_TIMER, NULL_QUERY_TIMER, NULL_QUERY_TIMER, None, None

    log_path = Path(timing.log_path) if timing.log_path else brain_root / "timing.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = _install_timing_handler(log_path)

    storage_timer = QueryTimer("sage.storage.timing", timing, "storage")
    content_timer = QueryTimer("sage.content.timing", timing, "content")
    retrieval_timer = QueryTimer("sage.retrieval.timing", timing, "retrieval")
    flusher = VaultTimingThread(
        timers=[storage_timer, content_timer, retrieval_timer],
        interval_seconds=timing.summary_interval_seconds,
    )
    flusher.start()
    return storage_timer, content_timer, retrieval_timer, flusher, handler


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
    # T-0073: per-vault background flusher for query-timing summary records.
    # None when timing is disabled or when the content store was injected
    # without going through _build_vault_timers (test paths).
    timing_thread: VaultTimingThread | None = None


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

    storage_timer, content_timer, retrieval_timer, timing_thread, _timing_handler = (
        _build_vault_timers(config.timing, brain_root)
    )

    graph_store = GraphStore(brain_root / "graph.db", query_timer=storage_timer)
    await graph_store.initialize(migrate=migrate)

    lock_manager = DocumentLockManager()

    # Content store: explicit instance > factory > production LanceDB.
    if content_store is None:
        if content_store_factory is not None:
            content_store = content_store_factory(brain_root)
        else:
            content_store = LanceDBContentStore(
                brain_root,
                migrate=migrate,
                query_timer=content_timer,
            )

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
            embedding_provider = get_nomic_embedding_provider()

    # Abstraction provider: injected, or factory-dispatched by config (T-0099).
    # SAGE_TEST_STUB_PROVIDERS=1 forces the stub regardless of config so that
    # tests cannot accidentally load Qwen3 (~16GB MLX/Metal) alongside the
    # running MCP server. The env var was originally added for nomic (T-0018)
    # and is extended to abstraction here as the interim guardrail for T-0029
    # against the kernel-panic-class failure documented in F-8.
    #
    # Dispatch contract:
    #   1. SAGE_TEST_STUB_PROVIDERS=1                 -> Stub (env override)
    #   2. config.abstraction.enabled is False        -> Stub (disabled gate;
    #      preserves ADR-011 opt-in semantics)
    #   3. config.abstraction.model is None           -> Stub (cannot
    #      construct a real provider without a model id)
    #   4. config.abstraction.provider == "stub"      -> Stub (explicit opt-out)
    #   5. config.abstraction.provider == "qwen3-mlx" -> Qwen3 (factory)
    if abstraction_provider is None:
        if (
            os.environ.get("SAGE_TEST_STUB_PROVIDERS") == "1"
            or not config.abstraction.enabled
            or config.abstraction.model is None
        ):
            abstraction_provider = StubAbstractionProvider()
        else:
            match config.abstraction.provider:
                case "stub":
                    abstraction_provider = StubAbstractionProvider()
                case "qwen3-mlx":
                    from sage.adapters.abstraction_qwen3 import (
                        get_qwen3_abstraction_provider,
                    )

                    abstraction_provider = get_qwen3_abstraction_provider(
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
    lifecycle_service = LifecycleService(graph_store, lock_manager, config, content_store)
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
        query_timer=retrieval_timer,
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
        timing_thread=timing_thread,
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
        if old.timing_thread is not None:
            old.timing_thread.stop(timeout=1.0)
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
