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

from sage.adapters.interfaces import (
    SYNTHETIC_HEADER_HEADING_PATH,
    ContentStore,
    EmbeddingProvider,
)
from sage.api.errors import (
    AssertionsFileInvalidError,
    AssertionsFileNotFoundError,
    AssertionsNotConfiguredError,
    DocumentNotFoundError,
    NoProjectionError,
    PathTraversalDeniedError,
    WritePathExistsError,
    WritePathInvalidError,
)
from sage.config import VaultConfig
from sage.models.schemas import (
    AssertionFailure,
    Document,
    EvalRetrievalResult,
    ExportProjectionResponse,
    ListHeadingsResponse,
    ReadMeta,
    ReadProjectionResponse,
    ReadSectionResponse,
    RefreshViewsResponse,
)
from sage.storage.graph_store import GraphStore

logger = logging.getLogger(__name__)


_MAX_CANDIDATE_MATCHES = 10


def _validate_write_to_path(write_to_path: str) -> None:
    """Verify write_to_path is an absolute path with a writable parent directory.

    Mirrors the path-validation discipline of
    ``sage.services.documents._deliver_to_path``: absolute-path check,
    target-must-not-exist check, parent-directory must-exist /
    must-be-dir / must-be-writable check. Raises the same typed errors
    so MCP and REST callers see a consistent envelope across the two
    write-to-disk surfaces.
    """
    target = Path(write_to_path)
    if not target.is_absolute():
        raise WritePathInvalidError(write_to_path, "path must be absolute")
    if target.exists():
        raise WritePathExistsError(write_to_path)
    parent = target.parent
    if not parent.exists():
        raise WritePathInvalidError(write_to_path, f"parent directory does not exist: {parent}")
    if not parent.is_dir():
        raise WritePathInvalidError(write_to_path, f"parent is not a directory: {parent}")
    if not os.access(parent, os.W_OK):
        raise WritePathInvalidError(write_to_path, f"parent directory is not writable: {parent}")


def _rank_candidate_matches(
    query: str,
    available: list[str] | None,
) -> list[str]:
    """Rank ``available`` heading paths by how directly they match ``query``.

    Returns at most ``_MAX_CANDIDATE_MATCHES`` paths sorted from best to
    worst fit. Used to populate ``candidate_matches`` in heading_not_found
    errors so callers can quickly pick the correct stored path. The
    ranking favors matches in the leaf segment over matches buried in
    parent segments, and shorter paths over longer ones — both heuristics
    point toward navigation targets and away from content paragraphs that
    happen to mention the query word.

    Tier 0: leaf segment equals query (case-insensitive, whitespace-trimmed)
    Tier 1: leaf segment starts with query (case-insensitive)
    Tier 2: leaf segment contains query as substring
    Tier 3: query appears in some parent segment but not the leaf
    No tier: query not found anywhere → not a candidate

    Within a tier, paths are ordered by ``len(heading_path)`` ascending.
    """
    if not available:
        return []
    needle = query.casefold().strip()
    if not needle:
        return []

    scored: list[tuple[int, int, str]] = []  # (tier, length, heading_path)
    for hp in available:
        segments = hp.split(" > ")
        leaf = segments[-1].casefold()
        leaf_stripped = leaf.strip()
        if leaf_stripped == needle:
            tier = 0
        elif leaf.startswith(needle) or leaf_stripped.startswith(needle):
            tier = 1
        elif needle in leaf:
            tier = 2
        elif any(needle in seg.casefold() for seg in segments[:-1]):
            tier = 3
        else:
            continue
        scored.append((tier, len(hp), hp))

    scored.sort(key=lambda t: (t[0], t[1]))
    return [hp for _, _, hp in scored[:_MAX_CANDIDATE_MATCHES]]


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

        # Exclude the synthetic header chunk so exported/read
        # projection text reflects only the body content the source
        # adapter produced — not the synthetic title/source/tags/abstract
        # surface added for retrieval.
        body_chunks = [c for c in chunks if c.heading_path != SYNTHETIC_HEADER_HEADING_PATH]
        projection_text = "\n\n".join(chunk.content for chunk in body_chunks)
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

    async def read_projection(
        self, document_id: str, write_to_path: str | None = None
    ) -> ReadProjectionResponse:
        """Read a document's full projection text with metadata.

        Two delivery modes:
        - write_to_path=None (default): return the complete projection
          inline in ``projection_text``, equivalent to uploading the
          source document.
        - write_to_path=/abs/path: SAGE writes the projection text bytes
          to the given absolute path. The response carries ``written_to``
          and ``content_size``; ``projection_text`` is null.

        The write_to_path mode mirrors ``DocumentsService.get_document_with_content``
        delivery and replaces the per--audit-removed
        ``export_projection`` MCP tool, whose pre-existing storage_root-
        relative semantics remain on the REST surface.

        Args:
            document_id: Document to read.
            write_to_path: Optional absolute filesystem path to write
                the projection text to. Must not already exist; parent
                directory must exist and be writable.

        Returns:
            ReadProjectionResponse with metadata and either inline
            projection text or write-delivery metadata.

        Raises:
            DocumentNotFoundError: Document does not exist.
            NoProjectionError: Document has no stored projection chunks.
            WritePathInvalidError: write_to_path is not absolute or its
                parent does not exist / is not writable.
            WritePathExistsError: write_to_path target already exists.
        """
        doc, projection_text = await self._get_projection_text(document_id)

        if write_to_path is None:
            return ReadProjectionResponse.from_document(doc, projection_text=projection_text)

        # write-to-disk delivery: validate path, write, return metadata-only response.
        _validate_write_to_path(write_to_path)
        data = projection_text.encode("utf-8")
        Path(write_to_path).write_bytes(data)

        response = ReadProjectionResponse.from_document(doc, projection_text=projection_text)
        response.projection_text = None
        response.written_to = write_to_path
        response.content_size = len(data)
        # Body was delivered to disk, not inline: no inline body present and
        # body_length is null for write-to-path delivery (CAS-ADR-039).
        response.read_meta = ReadMeta(success=True, body_present=False)
        return response

    # ------------------------------------------------------------------
    # read_section
    # ------------------------------------------------------------------

    async def read_section(self, document_id: str, heading_path: str) -> ReadSectionResponse:
        """Read a section's text by heading path with minimal metadata.

        Returns the joined text of all chunks matching the heading prefix,
        providing clean readable output for a document subsection without
        loading the full projection.

        Args:
            document_id: Document to read from.
            heading_path: Heading path prefix
                (e.g. "Technical Description > Composite Claim Binding").

        Returns:
            ReadSectionResponse with section text and metadata.

        Raises:
            DocumentNotFoundError: Document does not exist.
            NoProjectionError: Document has no stored projection chunks.
            HeadingNotFoundError: No chunks match the heading path.
        """
        from sage.api.errors import HeadingNotFoundError

        doc, _ = await self._get_projection_text(document_id)

        chunks = await self._content.get_chunks_by_heading_prefix(document_id, heading_path)
        if not chunks:
            available = await self._content.get_heading_paths(document_id)
            candidates = _rank_candidate_matches(heading_path, available)
            raise HeadingNotFoundError(
                heading_path,
                document_id,
                available_headings=available,
                candidate_matches=candidates if candidates else None,
            )

        section_text = "\n\n".join(chunk.content for chunk in chunks)

        return ReadSectionResponse.from_document(
            doc,
            heading_path=heading_path,
            chunk_count=len(chunks),
            section_text=section_text,
        )

    # ------------------------------------------------------------------
    # list_headings
    # ------------------------------------------------------------------

    async def list_headings(self, document_id: str) -> ListHeadingsResponse:
        """Return the distinct heading paths of a document in document order.

        Body content is not read; the column-only LanceDB select in
        get_heading_paths returns the structural table of contents without
        loading chunk content. Replaces the antipattern of harvesting
        available_headings from a deliberately-wrong read_section call.

        Args:
            document_id: Document to enumerate headings for.

        Returns:
            ListHeadingsResponse with the heading paths in document order.
            The synthetic header chunk is excluded by
            get_heading_paths so the returned paths are exactly those a
            caller may pass to read_section.

        Raises:
            DocumentNotFoundError: Document does not exist.
            NoProjectionError: Document has no stored projection chunks.
        """
        doc, _ = await self._get_projection_text(document_id)
        headings = await self._content.get_heading_paths(document_id)

        return ListHeadingsResponse.from_document(doc, headings=headings)

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
            raise AssertionsFileInvalidError(assertions_path, "Expected top-level 'assertions' key")

        assertions = raw["assertions"]
        if not isinstance(assertions, list):
            raise AssertionsFileInvalidError(assertions_path, "'assertions' must be a list")

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

                failures.append(
                    AssertionFailure(
                        query=query,
                        expected_document_id=expected_id,
                        top_k_checked=top_k,
                        found=False,
                        actual_rank=actual_rank,
                    )
                )

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
    def _create_source_symlink(storage_root: Path, link_dir: Path, source_path: str) -> None:
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
