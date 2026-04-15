"""Utility services: export_projection, eval_retrieval, refresh_views.

Covers behavioral tests BH-038 through BH-048.

export_projection: Write stored projection text to a Markdown file.
  - Path containment: output_path must resolve within storage_root (BH-038, BH-040).
  - Relative paths within storage_root are permitted (BH-039).

eval_retrieval: Run retrieval health assertions against the vault.
  - Assertions loaded from a separate YAML file (BH-041).
  - Missing or malformed file produces clear errors (BH-042).

refresh_views: Regenerate symlink-based browsable folder views.
  - Both by_doc_type/ and by_lifecycle/ views always generated (BH-043).
  - Symlinks target original source files via relative paths (BH-044).
  - Full regeneration on each call (BH-045).
  - Failed-pipeline documents included (BH-046).
  - Empty categories produce no directory (BH-047).
  - Null doc_type excluded from by_doc_type/ view (BH-048).
"""

import logging
import os
import shutil
from pathlib import Path

import yaml

from sage.adapters.interfaces import ContentStore, EmbeddingProvider
from sage.api.errors import (
    AssertionsFileInvalidError,
    AssertionsFileNotFoundError,
    AssertionsNotConfiguredError,
    DocumentNotFoundError,
    NoProjectionError,
    PathTraversalDeniedError,
)
from sage.config import VaultConfig
from sage.models.enums import PipelineStatus
from sage.models.schemas import (
    AssertionFailure,
    Document,
    EvalRetrievalResult,
    ExportProjectionResponse,
    ReadProjectionResponse,
    ReadSectionResponse,
    RefreshViewsResponse,
)
from sage.storage.graph_store import GraphStore

logger = logging.getLogger(__name__)


class UtilitiesService:
    def __init__(
        self,
        graph_store: GraphStore,
        content_store: ContentStore,
        embedding_provider: EmbeddingProvider,
        config: VaultConfig,
    ) -> None:
        self._graph = graph_store
        self._content = content_store
        self._embedding = embedding_provider
        self._config = config

    # ------------------------------------------------------------------
    # Shared projection retrieval
    # ------------------------------------------------------------------

    async def _get_projection_text(self, document_id: str) -> tuple[Document, str]:
        """Validate document exists and reconstruct projection text from chunks.

        Returns:
            (document, projection_text) tuple.

        Raises:
            DocumentNotFoundError: Document does not exist.
            NoProjectionError: Document has no stored projection chunks.
        """
        doc = await self._graph.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(document_id)

        chunks = await self._content.get_all_chunks(document_id)
        if not chunks:
            raise NoProjectionError(document_id)

        projection_text = "\n\n".join(chunk.content for chunk in chunks)
        return doc, projection_text

    # ------------------------------------------------------------------
    # export_projection (BH-038, BH-039, BH-040)
    # ------------------------------------------------------------------

    async def export_projection(
        self, document_id: str, output_path: str
    ) -> ExportProjectionResponse:
        """Export a document's projection to a Markdown file.

        Args:
            document_id: Document to export.
            output_path: File path (relative to storage_root or absolute).

        Returns:
            ExportProjectionResponse with the absolute output path.

        Raises:
            DocumentNotFoundError: Document does not exist.
            NoProjectionError: Document has no stored projection chunks.
            PathTraversalDeniedError: output_path resolves outside storage_root.
        """
        doc, projection_text = await self._get_projection_text(document_id)

        # Resolve and validate output path (BH-038, BH-039, BH-040)
        storage_root = Path(self._config.vault.storage_root).expanduser().resolve()
        target = (storage_root / output_path).resolve()

        # Path containment check
        if not str(target).startswith(str(storage_root) + "/") and target != storage_root:
            raise PathTraversalDeniedError(output_path)

        # Write the file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(projection_text, encoding="utf-8")

        return ExportProjectionResponse(
            document_id=document_id,
            output_path=str(target),
        )

    # ------------------------------------------------------------------
    # read_projection
    # ------------------------------------------------------------------

    async def read_projection(self, document_id: str) -> ReadProjectionResponse:
        """Read a document's full projection text with metadata.

        Returns the complete projection (reconstructed from stored chunks)
        directly, equivalent to uploading the source document.

        Args:
            document_id: Document to read.

        Returns:
            ReadProjectionResponse with metadata and full projection text.

        Raises:
            DocumentNotFoundError: Document does not exist.
            NoProjectionError: Document has no stored projection chunks.
        """
        doc, projection_text = await self._get_projection_text(document_id)

        return ReadProjectionResponse(
            document_id=document_id,
            title=doc.title,
            version_label=doc.version_label,
            lifecycle_status=doc.lifecycle_status,
            doc_type=doc.doc_type,
            source_path=doc.source_path,
            projection_text=projection_text,
        )

    # ------------------------------------------------------------------
    # read_section
    # ------------------------------------------------------------------

    async def read_section(
        self, document_id: str, heading_path: str
    ) -> ReadSectionResponse:
        """Read a section's text by heading path with minimal metadata.

        Returns the joined text of all chunks matching the heading prefix,
        providing clean readable output for a document subsection without
        loading the full projection.

        Args:
            document_id: Document to read from.
            heading_path: Heading path prefix (e.g. "Technical Description > Composite Claim Binding").

        Returns:
            ReadSectionResponse with section text and metadata.

        Raises:
            DocumentNotFoundError: Document does not exist.
            NoProjectionError: Document has no stored projection chunks.
            HeadingNotFoundError: No chunks match the heading path.
        """
        from sage.api.errors import HeadingNotFoundError

        doc, _ = await self._get_projection_text(document_id)

        chunks = await self._content.get_chunks_by_heading_prefix(
            document_id, heading_path
        )
        if not chunks:
            available = await self._content.get_heading_paths(document_id)
            raise HeadingNotFoundError(heading_path, document_id, available)

        section_text = "\n\n".join(chunk.content for chunk in chunks)

        return ReadSectionResponse(
            document_id=document_id,
            title=doc.title,
            heading_path=heading_path,
            chunk_count=len(chunks),
            section_text=section_text,
        )

    # ------------------------------------------------------------------
    # eval_retrieval (BH-041, BH-042)
    # ------------------------------------------------------------------

    async def eval_retrieval(self) -> EvalRetrievalResult:
        """Run retrieval health assertions against the vault.

        Loads assertions from the YAML file referenced in vault config,
        executes each as a semantic search, and returns a pass/fail report.

        Raises:
            AssertionsNotConfiguredError: No assertions_file in vault config.
            AssertionsFileNotFoundError: Referenced file does not exist.
            AssertionsFileInvalidError: File is malformed YAML or wrong structure.
        """
        # Check config
        rh_config = self._config.retrieval_health
        if rh_config is None or not rh_config.assertions_file:
            raise AssertionsNotConfiguredError()

        assertions_path = rh_config.assertions_file

        # Resolve relative to vault storage_root (where domain configs live)
        storage_root = Path(self._config.vault.storage_root).expanduser().resolve()
        full_path = (storage_root / assertions_path).resolve()

        if not full_path.exists():
            raise AssertionsFileNotFoundError(assertions_path)

        # Load and parse assertions
        try:
            with open(full_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise AssertionsFileInvalidError(assertions_path, str(exc))

        if not isinstance(raw, dict) or "assertions" not in raw:
            raise AssertionsFileInvalidError(
                assertions_path, "Expected top-level 'assertions' key"
            )

        assertions = raw["assertions"]
        if not isinstance(assertions, list):
            raise AssertionsFileInvalidError(
                assertions_path, "'assertions' must be a list"
            )

        # Run each assertion
        failures: list[AssertionFailure] = []
        for assertion in assertions:
            query = assertion.get("query")
            expected_id = assertion.get("expected_document_id")
            top_k = assertion.get("top_k", 10)

            if not query or not expected_id:
                raise AssertionsFileInvalidError(
                    assertions_path,
                    "Each assertion requires 'query' and 'expected_document_id'",
                )

            # Run semantic search
            embeddings = await self._embedding.embed([query])
            results = await self._content.search_semantic(embeddings[0], top_k)

            # Check if expected document is in results
            found = False
            actual_rank = None
            for rank, result in enumerate(results):
                if result.document_id == expected_id:
                    found = True
                    actual_rank = rank + 1
                    break

            if not found:
                # Check beyond top_k for diagnostic rank
                extended = await self._content.search_semantic(embeddings[0], top_k * 5)
                for rank, result in enumerate(extended):
                    if result.document_id == expected_id:
                        actual_rank = rank + 1
                        break

                failures.append(AssertionFailure(
                    query=query,
                    expected_document_id=expected_id,
                    top_k_checked=top_k,
                    found=False,
                    actual_rank=actual_rank,
                ))

        return EvalRetrievalResult(
            vault_id=self._config.vault.id,
            passed=len(failures) == 0,
            assertion_count=len(assertions),
            failure_count=len(failures),
            failures=failures,
        )

    # ------------------------------------------------------------------
    # refresh_views (BH-043 through BH-048)
    # ------------------------------------------------------------------

    async def refresh_views(self) -> RefreshViewsResponse:
        """Regenerate symlink-based browsable folder views.

        Creates two view dimensions under {storage_root}/views/:
          - by_doc_type/{doc_type}/ -- one directory per distinct doc_type
          - by_lifecycle/{lifecycle_status}/ -- one directory per distinct status

        Symlinks target the original source files via relative paths (BH-044).
        Full regeneration: the views/ directory is wiped and rebuilt (BH-045).
        Failed-pipeline documents are included (BH-046).
        Empty categories produce no directory (BH-047).
        Documents with null doc_type are excluded from by_doc_type/ (BH-048).
        """
        storage_root = Path(self._config.vault.storage_root).expanduser().resolve()
        views_root = storage_root / "views"

        # Full regeneration: wipe existing views (BH-045)
        if views_root.exists():
            shutil.rmtree(views_root)

        # Fetch all documents from graph store
        documents = await self._graph.list_all_documents()

        # Build view buckets
        by_doc_type: dict[str, list] = {}
        by_lifecycle: dict[str, list] = {}

        for doc in documents:
            # by_doc_type: skip null doc_type (BH-048)
            if doc.doc_type is not None:
                by_doc_type.setdefault(doc.doc_type, []).append(doc)

            # by_lifecycle: always has a value
            by_lifecycle.setdefault(doc.lifecycle_status, []).append(doc)

        views_generated = 0

        # Generate by_doc_type/ directories (BH-047: only non-empty)
        for doc_type, docs in by_doc_type.items():
            type_dir = views_root / "by_doc_type" / doc_type
            type_dir.mkdir(parents=True, exist_ok=True)
            for doc in docs:
                self._create_source_symlink(storage_root, type_dir, doc.source_path)
            views_generated += 1

        # Generate by_lifecycle/ directories (BH-047: only non-empty)
        for status, docs in by_lifecycle.items():
            status_dir = views_root / "by_lifecycle" / status
            status_dir.mkdir(parents=True, exist_ok=True)
            for doc in docs:
                self._create_source_symlink(storage_root, status_dir, doc.source_path)
            views_generated += 1

        return RefreshViewsResponse(
            vault_id=self._config.vault.id,
            views_generated=views_generated,
        )

    @staticmethod
    def _create_source_symlink(
        storage_root: Path, link_dir: Path, source_path: str
    ) -> None:
        """Create a relative symlink from link_dir to the source file.

        Symlink name is the original filename. If a name collision occurs
        (two documents with the same filename), append a numeric suffix.
        """
        source_abs = storage_root / source_path
        filename = source_abs.name

        link_path = link_dir / filename
        if link_path.exists() or link_path.is_symlink():
            # Name collision: append numeric suffix
            stem = source_abs.stem
            suffix = source_abs.suffix
            counter = 2
            while link_path.exists() or link_path.is_symlink():
                link_path = link_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        # Compute relative path from link_dir to source file (BH-044)
        rel_target = os.path.relpath(source_abs, link_dir)
        link_path.symlink_to(rel_target)
