"""Three-stage ingestion pipeline (BH-018 through BH-026).

Stage 1 (projection): Synchronous. Reads source file, produces structured text.
Stage 2 (indexing): Async background task. Chunks, embeds, stores in content store.
Stage 3 (abstraction): Async background task. Generates semantic abstract via LLM.

The ingest endpoint returns immediately after Stage 1 (BH-026).
"""

import asyncio
import hashlib
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sage.adapters.interfaces import AbstractionProvider, ContentStore, Chunk, EmbeddingProvider
from sage.config import VaultConfig
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document, IngestRequest
from sage.services.identity import generate_document_id
from sage.source_adapters.base import ProjectionResult, SourceAdapter
from sage.storage.graph_store import GraphStore
from sage.storage.locks import DocumentLockManager

logger = logging.getLogger(__name__)


class IngestionService:
    def __init__(
        self,
        graph_store: GraphStore,
        lock_manager: DocumentLockManager,
        content_store: ContentStore,
        embedding_provider: EmbeddingProvider,
        abstraction_provider: AbstractionProvider,
        config: VaultConfig,
        source_adapters: dict[SourceType, SourceAdapter] | None = None,
    ) -> None:
        self._store = graph_store
        self._locks = lock_manager
        self._content_store = content_store
        self._embedding = embedding_provider
        self._abstraction = abstraction_provider
        self._config = config
        self._adapters = source_adapters or {}

    def _ensure_vault_local(
        self, source_path: Path, storage_root: Path
    ) -> str:
        """Return a vault-relative path to the source file, copying it
        into ``{storage_root}/imports/`` if it lives outside the vault.

        Internal files (already under *storage_root*) are returned as-is
        with a normalized relative path.  External files are copied
        verbatim; on filename collision a content-hash suffix is appended.

        Returns:
            Vault-relative path string (e.g. ``patents/doc.md`` or
            ``imports/doc_a1b2c3d4.md``).
        """
        try:
            relative = source_path.relative_to(storage_root)
            return str(relative)
        except ValueError:
            pass  # external file -- fall through to import

        imports_dir = storage_root / "imports"
        imports_dir.mkdir(exist_ok=True)

        dest = imports_dir / source_path.name
        if dest.exists():
            # Collision: disambiguate with 8-char content hash
            content_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()[:8]
            stem = source_path.stem
            suffix = source_path.suffix
            dest = imports_dir / f"{stem}_{content_hash}{suffix}"

        shutil.copy2(source_path, dest)
        return str(dest.relative_to(storage_root))

    async def ingest(
        self, request: IngestRequest, vault_id: str
    ) -> tuple[Document, int]:
        """Execute Stage 1 synchronously, schedule Stages 2-3 as background tasks.

        Returns:
            (document, http_status_code): 201 for new, 200 for force re-ingestion.

        Raises:
            DuplicateContentError: Same source_path + hash exists, no force flag (BH-018).
            AdapterNotFoundError: No adapter for requested source type.
        """
        from sage.api.errors import AdapterNotFoundError, DuplicateContentError

        adapter = self._adapters.get(request.adapter)
        if adapter is None:
            raise AdapterNotFoundError(request.adapter)

        # Resolve source path: relative to storage_root, or absolute external
        storage_root = Path(self._config.vault.storage_root).expanduser().resolve()
        source_input = Path(request.source)

        if source_input.is_absolute():
            source_path = source_input
        else:
            source_path = storage_root / request.source

        if not source_path.exists():
            from sage.api.errors import SourceFileNotFoundError
            raise SourceFileNotFoundError(request.source)

        # Import external files into the vault (BH-053 through BH-057)
        source_path = source_path.resolve()
        vault_relative = self._ensure_vault_local(source_path, storage_root)

        # Stage 1: Projection (synchronous)
        projection = await adapter.project(
            storage_root / vault_relative, request.config
        )

        # Check for duplicates (BH-018, BH-019)
        existing = await self._store.find_by_source_path_and_hash(
            vault_relative, projection.content_hash
        )

        if existing is not None and not request.force:
            raise DuplicateContentError(existing.id, projection.content_hash)

        now = datetime.now(timezone.utc)

        source_modified_at_str = projection.metadata.get("source_modified_at")
        source_modified_at = (
            datetime.fromisoformat(source_modified_at_str)
            if source_modified_at_str
            else None
        )

        if existing is not None and request.force:
            # Force re-ingestion: reuse existing record (BH-019)
            updates = {
                "pipeline_status": PipelineStatus.PROJECTION_COMPLETE.value,
                "pipeline_error": None,
                "projected_at": now.isoformat(),
                "adapter_version": projection.adapter_version,
                "updated_at": now.isoformat(),
                "semantic_abstract": None,
                "indexed_at": None,
                "source_modified_at": source_modified_at_str,
            }
            doc = await self._store.update_document(existing.id, updates)
            http_status = 200

            # Remove old content store entries for re-indexing
            await self._content_store.remove_document(existing.id)
        else:
            # New document
            created_by = request.created_by or self._config.vault.owner
            doc_id = generate_document_id(
                vault_relative, now.isoformat(), projection.title
            )
            doc = Document(
                id=doc_id,
                title=projection.title,
                source_type=request.adapter,
                source_path=vault_relative,
                lifecycle_status="active",
                source_content_hash=projection.content_hash,
                adapter_version=projection.adapter_version,
                created_by=created_by,
                created_at=now,
                last_modified_by=created_by,
                updated_at=now,
                projected_at=now,
                source_modified_at=source_modified_at,
                pipeline_status=PipelineStatus.PROJECTION_COMPLETE,
            )
            await self._store.insert_document(doc)
            http_status = 201

        # Schedule background pipeline (Stages 2-3) (BH-026)
        asyncio.create_task(
            self._run_background_pipeline(doc.id, projection)
        )

        return doc, http_status

    async def _run_background_pipeline(
        self, document_id: str, projection: ProjectionResult
    ) -> None:
        """Run Stages 2-3 as a background task. Updates pipeline_status
        in the graph store as it progresses. Catches all exceptions and
        sets pipeline_status to failed with pipeline_error.
        """
        try:
            await self._stage2_indexing(document_id, projection)

            # Check abstraction config
            if not self._config.abstraction.enabled:
                # BH-025: abstraction_skipped
                async with self._locks.lock(document_id):
                    await self._store.update_document(document_id, {
                        "pipeline_status": PipelineStatus.ABSTRACTION_SKIPPED.value,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    })
                return

            await self._stage3_abstraction(document_id, projection)
        except Exception as exc:
            logger.exception(
                "Pipeline failed for document %s", document_id
            )
            async with self._locks.lock(document_id):
                await self._store.update_document(document_id, {
                    "pipeline_status": PipelineStatus.FAILED.value,
                    "pipeline_error": str(exc),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })

    async def _stage2_indexing(
        self, document_id: str, projection: ProjectionResult
    ) -> None:
        """Stage 2: Chunk projection, embed, store in content store.
        Sets indexed_at on completion (BH-008).
        """
        async with self._locks.lock(document_id):
            await self._store.update_document(document_id, {
                "pipeline_status": PipelineStatus.INDEXING_IN_PROGRESS.value,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        # Chunk the projection by heading
        chunks = self._chunk_projection(document_id, projection)

        # Embed all chunks
        if chunks:
            texts = [c.content for c in chunks]
            embeddings = await self._embedding.embed(texts)
            for chunk, embedding in zip(chunks, embeddings):
                chunk.embedding = embedding

        # Store in content store
        await self._content_store.index_chunks(document_id, chunks)

        # Mark indexing complete (BH-008)
        now = datetime.now(timezone.utc)
        async with self._locks.lock(document_id):
            await self._store.update_document(document_id, {
                "pipeline_status": PipelineStatus.INDEXING_COMPLETE.value,
                "indexed_at": now.isoformat(),
                "updated_at": now.isoformat(),
            })

    async def _stage3_abstraction(
        self, document_id: str, projection: ProjectionResult
    ) -> None:
        """Stage 3: Generate semantic abstract via LLM (BH-024, BH-025)."""
        async with self._locks.lock(document_id):
            await self._store.update_document(document_id, {
                "pipeline_status": PipelineStatus.ABSTRACTION_IN_PROGRESS.value,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        max_tokens = self._config.abstraction.max_abstract_tokens
        abstract = await self._abstraction.generate_abstract(
            projection.text, max_tokens
        )

        now = datetime.now(timezone.utc)
        async with self._locks.lock(document_id):
            await self._store.update_document(document_id, {
                "pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE.value,
                "semantic_abstract": abstract,
                "updated_at": now.isoformat(),
            })

    def _chunk_projection(
        self, document_id: str, projection: ProjectionResult
    ) -> list[Chunk]:
        """Split projection into chunks by heading."""
        chunks: list[Chunk] = []
        for i, heading in enumerate(projection.headings):
            if heading.content.strip():
                chunks.append(
                    Chunk(
                        document_id=document_id,
                        heading_path=heading.path,
                        content=heading.content,
                        chunk_index=i,
                    )
                )

        # If no headings, create a single chunk from the full text
        if not chunks and projection.text.strip():
            chunks.append(
                Chunk(
                    document_id=document_id,
                    heading_path="",
                    content=projection.text,
                    chunk_index=0,
                )
            )

        return chunks
