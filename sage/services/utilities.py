"""Utility services: export_projection and eval_retrieval.

Covers behavioral tests BH-038 through BH-042.

export_projection: Write stored projection text to a Markdown file.
  - Path containment: output_path must resolve within storage_root (BH-038, BH-040).
  - Relative paths within storage_root are permitted (BH-039).

eval_retrieval: Run retrieval health assertions against the vault.
  - Assertions loaded from a separate YAML file (BH-041).
  - Missing or malformed file produces clear errors (BH-042).
"""

import logging
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
    EvalRetrievalResult,
    ExportProjectionResponse,
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
        # Validate document exists
        doc = await self._graph.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(document_id)

        # Resolve and validate output path (BH-038, BH-039, BH-040)
        storage_root = Path(self._config.vault.storage_root).expanduser().resolve()
        target = (storage_root / output_path).resolve()

        # Path containment check
        if not str(target).startswith(str(storage_root) + "/") and target != storage_root:
            raise PathTraversalDeniedError(output_path)

        # Retrieve projection chunks from content store
        chunks = await self._content.get_all_chunks(document_id)
        if not chunks:
            raise NoProjectionError(document_id)

        # Reconstruct projection text from chunks
        projection_text = "\n\n".join(chunk.content for chunk in chunks)

        # Write the file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(projection_text, encoding="utf-8")

        return ExportProjectionResponse(
            document_id=document_id,
            output_path=str(target),
        )

    # ------------------------------------------------------------------
    # eval_retrieval (BH-041, BH-042)
    # ------------------------------------------------------------------

    async def eval_retrieval(self, vault_id: str) -> EvalRetrievalResult:
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
            vault_id=vault_id,
            passed=len(failures) == 0,
            assertion_count=len(assertions),
            failure_count=len(failures),
            failures=failures,
        )
