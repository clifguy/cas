"""Three-stage ingestion pipeline (BH-018 through BH-026, BH-068).

Stage 1 (projection): Reads source file, produces structured text.
Stage 2 (indexing): Chunks, embeds, stores in content store.
Stage 3 (abstraction): Generates semantic abstract via LLM.

All three stages run sequentially within ingest(). The method returns
after the full pipeline completes, keeping peak memory bounded to one
document at a time (BH-026, BH-068).
"""

import hashlib
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sage.adapters.abstraction_utils import compute_max_tokens, trim_to_sentence_boundary
from sage.adapters.interfaces import AbstractionProvider, ContentStore, Chunk, EmbeddingProvider
from sage.api.errors import (
    AdapterNotFoundError,
    DocumentNotFoundError,
    DuplicateContentError,
    NoProjectionError,
    SourceFileNotFoundError,
)
from sage.config import VaultConfig
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document, IngestRequest
from sage.services.identity import generate_document_id
from sage.source_adapters.base import ProjectionResult, SourceAdapter
from sage.storage.graph_store import GraphStore
from sage.storage.locks import DocumentLockManager

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    """Result of an ingestion operation."""
    document: Document
    is_new: bool


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

    @property
    def registered_adapters(self) -> dict[SourceType, SourceAdapter]:
        """Return the runtime adapter registry."""
        return dict(self._adapters)

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
            content_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()[:8]
            existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()[:8]
            if content_hash == existing_hash:
                # Identical content already imported -- reuse existing path
                return str(dest.relative_to(storage_root))
            # Different content: disambiguate with 8-char content hash
            stem = source_path.stem
            suffix = source_path.suffix
            dest = imports_dir / f"{stem}_{content_hash}{suffix}"

        shutil.copy2(source_path, dest)
        return str(dest.relative_to(storage_root))

    async def ingest(
        self, request: IngestRequest,
    ) -> IngestResult:
        """Execute Stage 1 synchronously, schedule Stages 2-3 as background tasks.

        Returns:
            IngestResult with the document and whether it was newly created.

        Raises:
            DuplicateContentError: Same content hash exists anywhere in vault (BH-066),
                or same source_path + hash exists (BH-018), unless force flag set.
            AdapterNotFoundError: No adapter for requested source type.
        """
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
            raise SourceFileNotFoundError(request.source)

        # Import external files into the vault (BH-053 through BH-057)
        source_path = source_path.resolve()
        vault_relative = self._ensure_vault_local(source_path, storage_root)

        # Stage 1: Projection (synchronous)
        projection = await adapter.project(
            storage_root / vault_relative, request.config
        )

        # Check for duplicates (BH-018, BH-019, BH-066, BH-067)
        if not request.force:
            # Hash-only check: catch identical content at any path (BH-066)
            hash_matches = await self._store.find_documents_by_hashes(
                [projection.content_hash]
            )
            if hash_matches:
                existing_id = next(iter(hash_matches.values()))
                raise DuplicateContentError(existing_id, projection.content_hash)

        # Path+hash check for force re-ingestion (reuses existing record)
        existing = await self._store.find_by_source_path_and_hash(
            vault_relative, projection.content_hash
        )

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
                "document_date": None,
            }
            doc = await self._store.update_document(existing.id, updates)
            is_new = False

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
            is_new = True

        # Apply caller-supplied metadata if provided
        if request.metadata:
            metadata_updates = self._build_metadata_updates(request.metadata)
            if metadata_updates:
                doc = await self._store.update_document(doc.id, metadata_updates)

        # Ensure every document has a doc_type for content-store pre-filtering.
        # Defaults to "misc" when neither caller metadata nor existing record
        # provides one (preserves existing doc_type on re-ingestion).
        if not doc.doc_type:
            doc = await self._store.update_document(doc.id, {"doc_type": "misc"})

        # Fallback: derive document_date from source_modified_at (BH-063)
        if not doc.document_date and source_modified_at:
            doc = await self._store.update_document(doc.id, {
                "document_date": source_modified_at.date().isoformat(),
            })

        # Run pipeline sequentially (Stages 2-3) (BH-026)
        # Sequential execution caps peak memory: only one document's
        # embeddings and abstraction context reside in memory at a time.
        await self._run_background_pipeline(doc.id, projection)

        # Re-fetch document to reflect terminal pipeline status
        doc = await self._store.get_document(doc.id) or doc

        return IngestResult(document=doc, is_new=is_new)

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

        # Build search preamble from document metadata (BH-058)
        doc = await self._store.get_document(document_id)
        preamble = self._build_search_preamble(doc) if doc else ""

        # Chunk the projection by heading
        chunks = self._chunk_projection(document_id, projection, preamble)

        # Stamp doc_type on chunks for content-store pre-filtering
        if doc and doc.doc_type and chunks:
            for chunk in chunks:
                chunk.doc_type = doc.doc_type

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

    async def _generate_abstract_text(self, text: str) -> str:
        """Generate a semantic abstract from document text.

        Shared core for both initial ingestion (stage 3) and reabstract.
        Computes a density-proportional token budget, invokes the
        abstraction provider, and trims the result to the last complete
        sentence boundary.

        Args:
            text: Full projection text of the document.

        Returns:
            Trimmed abstract string.
        """
        word_count = len(text.split())
        max_tokens = compute_max_tokens(word_count, self._config.abstraction)
        raw_abstract = await self._abstraction.generate_abstract(
            text, max_tokens
        )
        return trim_to_sentence_boundary(raw_abstract)

    async def _stage3_abstraction(
        self, document_id: str, projection: ProjectionResult
    ) -> None:
        """Stage 3: Generate semantic abstract via LLM (BH-024, BH-025)."""
        async with self._locks.lock(document_id):
            await self._store.update_document(document_id, {
                "pipeline_status": PipelineStatus.ABSTRACTION_IN_PROGRESS.value,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        abstract = await self._generate_abstract_text(projection.text)

        now = datetime.now(timezone.utc)
        async with self._locks.lock(document_id):
            await self._store.update_document(document_id, {
                "pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE.value,
                "semantic_abstract": abstract,
                "updated_at": now.isoformat(),
            })

    async def reabstract(self, document_id: str) -> Document:
        """Re-run abstraction on an existing document.

        Reconstructs projection text from stored chunks, generates a
        new abstract via _generate_abstract_text, and writes it back
        to the document node.

        Args:
            document_id: ID of the document to re-abstract.

        Returns:
            Updated Document with new semantic_abstract.

        Raises:
            DocumentNotFoundError: Document does not exist.
            NoProjectionError: Document has no stored projection chunks.
        """
        doc = await self._store.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(document_id)

        chunks = await self._content_store.get_all_chunks(document_id)
        if not chunks:
            raise NoProjectionError(document_id)

        projection_text = "\n\n".join(chunk.content for chunk in chunks)
        abstract = await self._generate_abstract_text(projection_text)

        now = datetime.now(timezone.utc)
        async with self._locks.lock(document_id):
            updated = await self._store.update_document(document_id, {
                "semantic_abstract": abstract,
                "updated_at": now.isoformat(),
            })
        return updated

    @staticmethod
    def _build_metadata_updates(metadata: dict[str, str]) -> dict:
        """Convert caller-supplied metadata dict to document field updates.

        Known fields are mapped to document columns. Unknown fields are
        stored but have no schema-enforced semantics.
        """
        KNOWN_FIELDS = {
            "title", "version_label", "project", "doc_type",
            "authority_scope", "document_date",
        }
        updates: dict = {}
        for key, value in metadata.items():
            if value is None:
                continue
            if key in KNOWN_FIELDS:
                updates[key] = value
            elif key == "date":
                # Filename-parsed date -> document_date (BH-062)
                updates["document_date"] = value
            elif key == "codes":
                # codes stored as comma-separated string -> tags list
                updates["tags"] = [c.strip() for c in value.split(",") if c.strip()]
        return updates

    def _chunk_projection(
        self, document_id: str, projection: ProjectionResult,
        search_preamble: str = "",
    ) -> list[Chunk]:
        """Split projection into chunks by heading.

        Prepends a search preamble to the first chunk so that document
        identity signals (title, source filename, tags) are indexed in
        both BM25 and vector search (BH-058).
        """
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

        # Prepend search preamble to the first chunk for indexing (BH-058)
        if chunks and search_preamble:
            chunks[0].content = search_preamble + chunks[0].content

        return chunks

    @staticmethod
    def _build_search_preamble(doc: Document) -> str:
        """Build a search preamble from document metadata (BH-058).

        Includes title, source filename, and tags so they are indexed
        for BM25 keyword search and vector similarity.
        """
        parts: list[str] = []
        if doc.title:
            parts.append(f"Title: {doc.title}")
        # Source filename contains codes like PV07, REF, etc.
        if doc.source_path:
            filename = doc.source_path.rsplit("/", 1)[-1]
            # Strip extension for cleaner indexing
            stem = filename.rsplit(".", 1)[0] if "." in filename else filename
            parts.append(f"Source: {stem}")
        if doc.tags:
            parts.append(f"Tags: {', '.join(doc.tags)}")
        if not parts:
            return ""
        return "\n".join(parts) + "\n\n"
