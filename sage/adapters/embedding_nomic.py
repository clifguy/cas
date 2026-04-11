"""nomic-embed-text EmbeddingProvider implementation.

Uses sentence-transformers to load nomic-ai/nomic-embed-text-v1.5.
Produces 768-dimensional L2-normalized embeddings.
"""

import logging

from sage.adapters.interfaces import EmbeddingProvider

logger = logging.getLogger(__name__)

NOMIC_MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"
EXPECTED_DIMENSIONS = 768


class NomicEmbeddingProvider(EmbeddingProvider):
    """Production embedding provider using nomic-embed-text-v1.5.

    Loads the model eagerly at init and validates output dimensions
    with a probe embedding. Raises on model load failure (AD-008).
    All output vectors are L2-normalized (AD-004).
    """

    def __init__(self, model_name: str = NOMIC_MODEL_NAME) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers is required for NomicEmbeddingProvider. "
                "Install with: pip install sentence-transformers"
            ) from exc

        logger.info("Loading embedding model: %s (device=cpu)", model_name)
        try:
            # Force CPU to avoid MPS memory contention on Apple Silicon
            # unified memory. MPS attention tensors scale quadratically
            # with sequence length and can exhaust the shared memory pool.
            self._model = SentenceTransformer(
                model_name, trust_remote_code=True, device="cpu"
            )
            # Cap sequence length to 2048 (nomic's primary training context).
            # The default 8192 produces attention matrices 16x larger.
            # Texts beyond 2048 tokens are truncated; the leading content
            # (title, headings, opening paragraphs) is preserved.
            self._model.max_seq_length = 2048
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load embedding model '{model_name}': {exc}"
            ) from exc

        # Validate dimensions with a probe (AD-001)
        probe = self._model.encode(
            ["dimension probe"], normalize_embeddings=True
        )
        actual_dim = probe.shape[1]
        if actual_dim != EXPECTED_DIMENSIONS:
            raise RuntimeError(
                f"Expected {EXPECTED_DIMENSIONS} dimensions from {model_name}, "
                f"got {actual_dim}"
            )
        self._dimensions = EXPECTED_DIMENSIONS
        logger.info(
            "Embedding model loaded: %s (%d dimensions)",
            model_name, self._dimensions,
        )

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns L2-normalized vectors.

        Empty input returns empty output immediately (AD-006).
        Order is preserved (AD-002).
        """
        if not texts:
            return []

        # sentence-transformers encode is synchronous; call directly
        # since it releases the GIL during model inference.
        # batch_size=8 bounds per-batch memory for long sequences
        # (attention scales quadratically with sequence length).
        embeddings = self._model.encode(
            texts, normalize_embeddings=True, batch_size=8
        )
        return [vec.tolist() for vec in embeddings]
