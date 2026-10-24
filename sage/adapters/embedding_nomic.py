"""nomic-embed-text EmbeddingProvider implementation.

Uses sentence-transformers to load nomic-ai/nomic-embed-text-v1.5.
Produces 768-dimensional L2-normalized embeddings.

The model runs remote code that lives in a second repository, named in the
model's ``auto_map``. Both the weights and that code are pinned to commits,
and the code files are checked against pinned SHA-256 digests before they
are imported; a mismatch refuses the load.
"""

import asyncio
import contextvars
import hashlib
import logging
import os
import threading
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from sage.adapters.interfaces import EmbeddingProvider
from sage.adapters.tokenization import require_bounded_tokenizer_input
from sage.instrumentation.performance import current_trace, span

logger = logging.getLogger(__name__)

NOMIC_MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"
EXPECTED_DIMENSIONS = 768
# The sequence length inference is capped at, and so the bound past which input
# is truncated. nomic's primary training context; the model's default of 8192
# produces attention matrices 16x larger.
MAX_INPUT_TOKENS = 2048
# Pairing uses byte-based WordPiece upper bounds, never request-wide tokenization.
MAX_BATCH_TEXTS = 2
MAX_BATCH_LOOKAHEAD = 8
MAX_BATCH_RAW_BYTES = 4096
MAX_BATCH_EFFECTIVE_TOKENS = 2048
MAX_BATCH_PADDED_TOKENS = 3072
MAX_BATCH_ATTENTION_WORK = MAX_INPUT_TOKENS**2

# Construction is guarded too: __new__-based test providers and concurrent first
# users must obtain the same provider-local lock without relying on the GIL.
_TOKENIZER_LOCK_CREATION_LOCK = threading.Lock()


@dataclass(frozen=True)
class ModelPin:
    """Immutable identity of a remote-code embedding model.

    ``revision`` is the model repository commit. ``code_repo`` and
    ``code_revision`` locate the remote code the model's ``auto_map`` names,
    which a model revision alone does not pin when the code lives in another
    repository. ``code_sha256`` maps each remote-code file to the digest it
    must have before it may be imported.
    """

    revision: str
    code_repo: str
    code_revision: str
    code_sha256: Mapping[str, str]


MODEL_PINS: dict[str, ModelPin] = {
    NOMIC_MODEL_NAME: ModelPin(
        revision="e9b6763023c676ca8431644204f50c2b100d9aab",
        code_repo="nomic-ai/nomic-bert-2048",
        code_revision="7710840340a098cfb869c4f65e87cf2b1b70caca",
        code_sha256={
            "configuration_hf_nomic_bert.py": (
                "f7871694b8de3d3df4ac6640313d5799ce323261a0fb90c5cc567ecc34a0039e"
            ),
            "modeling_hf_nomic_bert.py": (
                "3b24a366c4cc31b869466ccfb7bbb8879e138c97f8de06c83d4fa1e31a21f149"
            ),
        },
    ),
}


def _sha256(path: str | os.PathLike[str]) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _imported_copy_path(pin: ModelPin, filename: str) -> Path:
    """Where transformers keeps the copy of a remote-code file it imports.

    For a Hub repository, transformers reuses an existing copy at the pinned
    commit without comparing it to the downloaded snapshot, so that copy,
    not the snapshot, is what executes.
    """
    from transformers.dynamic_module_utils import _sanitize_module_name
    from transformers.utils import hub as transformers_hub

    parts = [_sanitize_module_name(part) for part in pin.code_repo.split("/")]
    return Path(
        transformers_hub.HF_MODULES_CACHE,
        transformers_hub.TRANSFORMERS_DYNAMIC_MODULE_NAME,
        *parts,
        pin.code_revision,
        filename,
    )


def _verify_remote_code(pin: ModelPin) -> None:
    """Refuse unless every remote-code file matches its pinned digest.

    Runs before anything is imported. Checks the file as fetched at the
    pinned code revision and, where one already exists, the copy the
    loader would import in its place.
    """
    import huggingface_hub

    for filename, expected in pin.code_sha256.items():
        try:
            fetched = huggingface_hub.hf_hub_download(
                pin.code_repo, filename, revision=pin.code_revision
            )
        except Exception as exc:
            raise RuntimeError(
                f"Remote code {pin.code_repo}/{filename} at {pin.code_revision} "
                f"is unavailable: {exc}"
            ) from exc
        candidates = [Path(fetched)]
        imported_copy = _imported_copy_path(pin, filename)
        if imported_copy.exists():
            candidates.append(imported_copy)
        for candidate in candidates:
            actual = _sha256(candidate)
            if actual != expected:
                raise RuntimeError(
                    f"Remote code {candidate} has SHA-256 {actual}; the pinned digest is {expected}"
                )


def _verify_loaded_code(model: Any, pin: ModelPin) -> None:
    """Confirm the classes actually imported come from the pinned, verified code."""
    import inspect

    auto_model = model[0].auto_model
    for cls in (type(auto_model), type(auto_model.config)):
        not_pinned = RuntimeError(
            f"Loaded class {cls.__module__}.{cls.__qualname__} is not from "
            f"{pin.code_repo} at {pin.code_revision}"
        )
        if pin.code_revision not in cls.__module__.split("."):
            raise not_pinned
        try:
            source = Path(inspect.getfile(cls))
        except (TypeError, OSError) as exc:
            raise not_pinned from exc
        expected = pin.code_sha256.get(source.name)
        if expected is None:
            raise not_pinned
        if _sha256(source) != expected:
            raise RuntimeError(
                f"Loaded class {cls.__qualname__} has a SHA-256 that differs from the pin"
            )


def _build_pinned_model(model_name: str, pin: ModelPin) -> Any:
    """Assemble the sentence-transformers pipeline from its modules.

    The plain ``SentenceTransformer(name, ...)`` constructor removes
    ``code_revision`` from ``model_kwargs`` while resolving the module
    classes, so the model load receives none and fetches the remote code
    from its repository's default branch. Building the ``Transformer`` module
    directly delivers the pinned code revision to both the configuration and
    the model load.
    """
    import huggingface_hub
    from sentence_transformers import SentenceTransformer
    from sentence_transformers.sentence_transformer.modules import Pooling, Transformer

    pinned = {
        "revision": pin.revision,
        "code_revision": pin.code_revision,
        "trust_remote_code": True,
    }
    transformer = Transformer(
        model_name,
        max_seq_length=MAX_INPUT_TOKENS,
        model_kwargs=dict(pinned),
        config_kwargs=dict(pinned),
        processor_kwargs={"revision": pin.revision},
    )
    pooling_config = huggingface_hub.hf_hub_download(
        model_name, "1_Pooling/config.json", revision=pin.revision
    )
    pooling = Pooling.load(os.path.dirname(pooling_config))
    # Force CPU to avoid MPS memory contention on Apple Silicon unified
    # memory. MPS attention tensors scale quadratically with sequence length
    # and can exhaust the shared memory pool.
    return SentenceTransformer(modules=[transformer, pooling], device="cpu")


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
            import sentence_transformers  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers is required for NomicEmbeddingProvider. "
                "Install with: pip install sentence-transformers"
            ) from exc

        self._model_name = model_name
        pin = MODEL_PINS.get(model_name)
        if pin is None:
            raise RuntimeError(
                f"Failed to load embedding model '{model_name}': no pinned "
                f"revision is declared for it"
            )
        logger.info("Loading embedding model: %s at %s (device=cpu)", model_name, pin.revision)
        # Quiet the cosmetic ``<All keys matched successfully>`` WARNING the
        # model load emits; install here, where the process's log handlers are
        # in place, immediately before the load.
        _install_nomic_keys_matched_filter()
        try:
            _verify_remote_code(pin)
            # Texts beyond the cap are truncated. Chunking keeps each passage
            # within it, so truncation reaches only text not built by chunking.
            self._model = _build_pinned_model(model_name, pin)
            _verify_loaded_code(self._model, pin)
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

    @property
    def max_input_tokens(self) -> int:
        return MAX_INPUT_TOKENS

    def _tokenizer_work_lock(self) -> threading.Lock:
        """Share mutable tokenizer state between sizing and inference workers."""
        with _TOKENIZER_LOCK_CREATION_LOCK:
            if not hasattr(self, "_tokenizer_lock"):
                self._tokenizer_lock = threading.Lock()
            return self._tokenizer_lock

    def count_tokens(self, text: str) -> int:
        """Count as ``encode`` does: WordPiece tokens plus the two special tokens.

        A WordPiece token never spans less than one character, so the count
        never exceeds the UTF-8 byte length plus two.
        """
        require_bounded_tokenizer_input(text)
        # The fast tokenizer changes its truncation state for each call. The
        # passage worker and inference worker must not borrow it concurrently.
        # Production sizing calls run off-loop, so waiting does not stop requests.
        with span("sizing.tokenizer_lock"):
            self._tokenizer_work_lock().acquire()
        try:
            with span("sizing.tokenize"):
                return len(self._model.tokenizer(text)["input_ids"])
        finally:
            self._tokenizer_work_lock().release()

    def _iter_batches(self, texts: list[str]) -> Iterator[tuple[list[int], list[str]]]:
        """Pair the oldest pending input within fixed, bounded lookahead windows.

        UTF-8 bytes plus two bounds WordPiece length including special tokens.
        No input is clipped or tokenized for packing. Each dispatch advances the
        oldest unscheduled index; deferred inputs never cross a window boundary.
        Original indices restore caller order after nonconsecutive pairing.
        """
        for start in range(0, len(texts), MAX_BATCH_LOOKAHEAD):
            pending = [
                (index, texts[index])
                for index in range(start, min(start + MAX_BATCH_LOOKAHEAD, len(texts)))
            ]
            while pending:
                first = pending.pop(0)
                batch = [first]
                for position, other in enumerate(pending):
                    pair = [first, other]
                    raw = [len(text.encode("utf-8")) for _, text in pair]
                    lengths = [min(size + 2, MAX_INPUT_TOKENS) for size in raw]
                    if (
                        len(pair) <= MAX_BATCH_TEXTS
                        and sum(raw) <= MAX_BATCH_RAW_BYTES
                        and sum(lengths) <= MAX_BATCH_EFFECTIVE_TOKENS
                        and len(pair) * max(lengths) <= MAX_BATCH_PADDED_TOKENS
                        and len(pair) * max(lengths) ** 2 <= MAX_BATCH_ATTENTION_WORK
                    ):
                        batch.append(pending.pop(position))
                        break
                yield [index for index, _ in batch], [text for _, text in batch]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns L2-normalized vectors.

        Empty input returns empty output immediately (AD-006).
        Order is preserved (AD-002).

        Inference is dispatched one bounded batch at a time, fairly sharing
        the dedicated single-thread executor without freezing the event loop. Releasing the GIL
        inside the C extension does not unblock the loop on its own: the
        loop runs on the calling thread, so a direct call would stall every
        concurrent request for the encode's full duration. The single
        worker thread keeps inference on one thread and serializes encodes.
        """
        if not texts:
            return []
        # Model truncation caps inference, but its tokenizer still allocates
        # against the original text. Crowded-heading exceptions and other
        # callers must fail through the existing pipeline failure path rather
        # than materializing unbounded tokenizer state or silently clipping.
        for text in texts:
            require_bounded_tokenizer_input(text)

        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sage-embedding")
        loop = asyncio.get_running_loop()
        # Admit only one quantum per caller, with at most two submitted jobs
        # (one running, one queued). Semaphore waiters are FIFO: both bulk and
        # interactive callers rejoin the tail after every batch. The executor
        # never retains the rest of a bulk list or its accumulated results.
        if not hasattr(self, "_inference_slots"):
            self._inference_slots = asyncio.Semaphore(2)
            self._inference_loop = loop
            self._inference_jobs = []
        elif self._inference_loop.is_closed():
            # Publish the new loop/capacity synchronously, before any caller
            # awaits. Running jobs from the closed loop retain their slots;
            # observe their actual completion on this loop without propagating
            # an abandoned caller's error into a new request.
            pending = [job for job in self._inference_jobs if not job.done()]
            self._inference_slots = asyncio.Semaphore(2 - len(pending))
            self._inference_loop = loop
            self._inference_jobs = pending
            for submitted in pending:
                work = asyncio.wrap_future(submitted, loop=loop)
                work.add_done_callback(partial(self._complete_inference, self._inference_slots))
        result: list[list[float]] = [[] for _ in texts]
        for indices, batch in self._iter_batches(texts):
            with span("embedding.admission"):
                await self._inference_slots.acquire()
            try:
                abandoned = threading.Event()
                trace = current_trace()
                context = contextvars.copy_context()
                queued_ns = time.perf_counter_ns()
                if trace is not None:
                    trace.retain()

                def run(
                    batch: list[str] = batch,
                    abandoned: threading.Event = abandoned,
                    trace=trace,
                    queued_ns: int = queued_ns,
                ) -> list[list[float]]:
                    if trace is not None:
                        trace.observe("embedding.executor", queued_ns, time.perf_counter_ns())
                    try:
                        return self._embed_quantum_sync(batch, abandoned)
                    finally:
                        if trace is not None:
                            trace.finish()

                try:
                    submitted = self._executor.submit(context.run, run)
                except BaseException:
                    if trace is not None:
                        trace.finish()
                    raise
                self._inference_jobs = [job for job in self._inference_jobs if not job.done()]
                self._inference_jobs.append(submitted)
                work = asyncio.wrap_future(submitted, loop=loop)
            except BaseException:
                self._inference_slots.release()
                raise

            # Shield the actual work from caller cancellation. Its slot belongs
            # to the worker until encode completes, even if the waiter leaves.
            # No subsequent quantum is submitted by a cancelled caller.
            work.add_done_callback(partial(self._complete_inference, self._inference_slots))
            try:
                vectors = await asyncio.shield(work)
                for index, vector in zip(indices, vectors, strict=True):
                    result[index] = vector
            except asyncio.CancelledError:
                # Leave the executor future alive until its queue item has
                # actually drained. Cancelling that future would release the
                # slot while retaining its cancelled queue item, allowing an
                # unbounded cancelled-work backlog behind a slow encode.
                abandoned.set()
                raise
        return result

    @staticmethod
    def _complete_inference(
        slots: asyncio.Semaphore, future: asyncio.Future[list[list[float]]]
    ) -> None:
        """Release admission only once the actual executor queue item drains."""
        slots.release()
        if not future.cancelled():
            future.exception()  # consume failures after abandoned awaits

    def _embed_quantum_sync(
        self, texts: list[str], abandoned: threading.Event
    ) -> list[list[float]]:
        """Drain abandoned queue items without borrowing model/tokenizer state."""
        if abandoned.is_set():
            trace = current_trace()
            if trace is not None:
                trace.count("embedding.abandoned", len(texts))
            return []
        return self._embed_sync(texts)

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        """Blocking embedding inference. Runs on the dedicated executor.

        Public dispatch supplies at most one conservatively bounded pair.
        The model's internal batch size also bounds direct benchmark calls.
        """
        with span("embedding.tokenizer_lock"):
            self._tokenizer_work_lock().acquire()
        try:
            trace = current_trace()
            hook_name = "preprocess" if hasattr(self._model, "preprocess") else "tokenize"
            original = getattr(self._model, hook_name, None)
            owned = hook_name in getattr(self._model, "__dict__", {})
            if trace is not None:
                trace.count("embedding.texts", len(texts))
                trace.count("embedding.characters", sum(map(len, texts)))
            if trace is not None and callable(original):

                def tokenize(*args: Any, **kwargs: Any) -> Any:
                    with span("embedding.tokenize"):
                        features = original(*args, **kwargs)
                    mask = features.get("attention_mask")
                    if mask is not None:
                        trace.count("embedding.tokens", int(mask.sum().item()))
                        for length in mask.sum(-1).tolist():
                            trace.extent("embedding.token_length", int(length))
                        trace.count("embedding.tokenized_batches", 1)
                    return features

                setattr(self._model, hook_name, tokenize)
            try:
                # Includes tokenization; its separately measured child stage
                # must not be added again to this inclusive duration.
                with span("embedding.encode"):
                    embeddings = self._model.encode(texts, normalize_embeddings=True, batch_size=8)
            finally:
                if trace is not None and callable(original):
                    if owned:
                        setattr(self._model, hook_name, original)
                    else:
                        delattr(self._model, hook_name)
            return [vec.tolist() for vec in embeddings]
        finally:
            self._tokenizer_work_lock().release()


# ── Process-level singleton ─────────────────────────────────
#
# initialize_services() in sage/mcp_init.py used to construct a fresh
# NomicEmbeddingProvider per vault; with N discovered vaults, the server
# paid N model loads (~270 MB each) and emitted N huggingface INFO dumps
# at startup. Inference is shared across vaults; the provider serializes access
# to the tokenizer's mutable state, so a single process-level instance suffices.

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
