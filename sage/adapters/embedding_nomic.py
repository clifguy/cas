"""nomic-embed-text EmbeddingProvider implementation.

Uses sentence-transformers to load nomic-ai/nomic-embed-text-v1.5.
Produces 768-dimensional L2-normalized embeddings.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from sage.adapters.interfaces import EmbeddingProvider

logger = logging.getLogger(__name__)

NOMIC_MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"
EXPECTED_DIMENSIONS = 768

# PyTorch's ``_IncompatibleKeys`` success repr, emitted as a cosmetic WARNING
# by nomic's remote modeling code on a clean state-dict load.
_KEYS_MATCHED_MESSAGE = "<All keys matched successfully>"


class _NomicKeysMatchedFilter(logging.Filter):
    """Swallow the cosmetic ``<All keys matched successfully>`` WARNING that
    nomic-embed-text's remote modeling code emits on a clean state-dict load.

    The match is narrowed to that exact message at WARNING level. A real key
    mismatch uses a longer ``_IncompatibleKeys(...)`` repr and stays visible,
    as does nomic's genuinely-useful ``scaled_dot_product_attention not
    available`` note.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.WARNING:
            return True
        return record.getMessage() != _KEYS_MATCHED_MESSAGE


_keys_matched_filter = _NomicKeysMatchedFilter()


def _install_nomic_keys_matched_filter() -> None:
    """Attach the content filter to the root logger's handlers (idempotent).

    nomic logs the warning through a *named* logger whose name embeds the HF
    snapshot hash, so the durable, name-independent suppression is a content
    filter on the *handler* that prints the propagated record -- an ancestor
    logger's own filters are not consulted for records that propagate up from a
    named child logger. Called right before the eager model load, when the
    process's root handler is already in place. Falls back to
    ``logging.lastResort`` when the root logger carries no handlers, so a
    caller that has not configured logging is covered too.
    """
    targets = list(logging.getLogger().handlers) or [logging.lastResort]
    for handler in targets:
        if handler is not None and not any(
            isinstance(existing, _NomicKeysMatchedFilter) for existing in handler.filters
        ):
            handler.addFilter(_keys_matched_filter)


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

        self._model_name = model_name
        logger.info("Loading embedding model: %s (device=cpu)", model_name)
        # Quiet the cosmetic ``<All keys matched successfully>`` WARNING the
        # model load emits; install here, where the process's log handlers are
        # in place, immediately before the load.
        _install_nomic_keys_matched_filter()
        try:
            # Force CPU to avoid MPS memory contention on Apple Silicon
            # unified memory. MPS attention tensors scale quadratically
            # with sequence length and can exhaust the shared memory pool.
            self._model = SentenceTransformer(model_name, trust_remote_code=True, device="cpu")
            # Cap sequence length to 2048 (nomic's primary training context).
            # The default 8192 produces attention matrices 16x larger.
            # Texts beyond 2048 tokens are truncated; the leading content
            # (title, headings, opening paragraphs) is preserved.
            self._model.max_seq_length = 2048
        except Exception as exc:
            raise RuntimeError(f"Failed to load embedding model '{model_name}': {exc}") from exc

        # Validate dimensions with a probe (AD-001)
        probe = self._model.encode(["dimension probe"], normalize_embeddings=True)
        actual_dim = probe.shape[1]
        if actual_dim != EXPECTED_DIMENSIONS:
            raise RuntimeError(
                f"Expected {EXPECTED_DIMENSIONS} dimensions from {model_name}, got {actual_dim}"
            )
        self._dimensions = EXPECTED_DIMENSIONS
        logger.info(
            "Embedding model loaded: %s (%d dimensions)",
            model_name,
            self._dimensions,
        )

        # Dedicated single-thread executor for blocking inference. Created
        # lazily on first embed. max_workers=1 keeps every encode on one
        # thread and serializes them; see embed.
        self._executor: ThreadPoolExecutor | None = None

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns L2-normalized vectors.

        Empty input returns empty output immediately (AD-006).
        Order is preserved (AD-002).

        The synchronous ``encode`` runs on a dedicated single-thread
        executor so it never freezes the event loop. Releasing the GIL
        inside the C extension does not unblock the loop on its own: the
        loop runs on the calling thread, so a direct call would stall every
        concurrent request for the encode's full duration. The single
        worker thread keeps inference on one thread and serializes encodes.
        """
        if not texts:
            return []

        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sage-embedding")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._embed_sync, texts)

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        """Blocking embedding inference. Runs on the dedicated executor.

        batch_size=8 bounds per-batch memory for long sequences (attention
        scales quadratically with sequence length).
        """
        embeddings = self._model.encode(texts, normalize_embeddings=True, batch_size=8)
        return [vec.tolist() for vec in embeddings]


# ── Process-level singleton ─────────────────────────────────
#
# initialize_services() in sage/mcp_init.py used to construct a fresh
# NomicEmbeddingProvider per vault; with N discovered vaults, the server
# paid N model loads (~270 MB each) and emitted N huggingface INFO dumps
# at startup. nomic-embed-text is stateless inference and shared safely
# across vaults, so a single process-level instance suffices.

_singleton: NomicEmbeddingProvider | None = None


def get_nomic_embedding_provider(
    model_name: str = NOMIC_MODEL_NAME,
) -> NomicEmbeddingProvider:
    """Return the process-wide NomicEmbeddingProvider, constructing on first call.

    Subsequent calls return the cached instance. Requesting a different
    ``model_name`` than the cached instance raises ``RuntimeError`` rather
    than silently loading a second model — vaults are expected to agree on
    the embedding provider per CAS design.
    """
    global _singleton
    if _singleton is None:
        _singleton = NomicEmbeddingProvider(model_name=model_name)
    elif _singleton._model_name != model_name:
        raise RuntimeError(
            f"NomicEmbeddingProvider singleton already initialized with "
            f"model_name={_singleton._model_name!r}; cannot satisfy request "
            f"for model_name={model_name!r}. nomic-embed-text is intended to "
            f"be SAGE-stack-wide; reconcile vault configs or call "
            f"_reset_nomic_singleton() if intentional."
        )
    return _singleton


def _reset_nomic_singleton() -> None:
    """Clear the cached provider. For test isolation only."""
    global _singleton
    _singleton = None
