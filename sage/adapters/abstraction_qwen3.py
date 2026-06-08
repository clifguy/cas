"""Qwen3 AbstractionProvider implementation via MLX.

Uses mlx-lm to load Qwen3-30B-A3B-Instruct-2507 (or compatible model)
and generate density-proportional semantic abstracts on Apple Silicon.
"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from sage.adapters.abstraction_prompt import (
    SYSTEM_PROMPT_TEMPLATE,  # noqa: F401 -- re-exported for callers importing from this module
    _format_system_prompt,
)
from sage.adapters.interfaces import AbstractionProvider
from sage.utils.unified_memory import (
    UnifiedMemoryExhaustedError,
    free_unified_memory_bytes,
    min_free_bytes,
)

logger = logging.getLogger(__name__)

# Module-level lock serializing all MLX generate_abstract calls (
# guardrail 2). Two concurrent ingest pipelines previously could both
# drive Qwen3 toward the unified-memory ceiling at once; this lock
# enforces single-flight discipline. It is scoped to this local provider:
# hosted providers carry no unified-memory budget and must not serialize
# their network calls behind it.
_generation_lock = asyncio.Lock()


DEFAULT_CONTEXT_WINDOW = 32768


class Qwen3AbstractionProvider(AbstractionProvider):
    """Production abstraction provider using Qwen3 via MLX.

    Defers model loading to first use (AD-026 revised). Construction stores
    configuration only; the MLX model loads on the first generate_abstract()
    call, validated by a probe generation. This reduces baseline memory by
    ~16-20 GB when abstraction has not yet been invoked.

    Uses greedy decoding via make_sampler(temp=0) for deterministic output
    (AD-029). Truncates long input to fit the context window, preserving
    leading content (AD-031).
    """

    def __init__(
        self,
        model_id: str,
        context_window: int = DEFAULT_CONTEXT_WINDOW,
    ) -> None:
        self._model_id = model_id
        self._context_window = context_window

        # Deferred state: populated by _ensure_loaded() on first use
        self._model = None
        self._tokenizer = None
        self._generate_fn = None
        self._greedy_sampler = None

        # Dedicated single-thread executor for the blocking model load and
        # inference. Created lazily on first generate_abstract and released
        # in unload(). max_workers=1 pins every model operation to one
        # thread and serializes inference; see generate_abstract.
        self._executor: ThreadPoolExecutor | None = None

        # Idle tracker for the eviction policy. Updated at the
        # end of every successful generate_abstract; consulted by
        # evict_if_idle. None means "never served a call since the
        # last (re)load" — treated as "not idle" rather than
        # "infinitely idle" so a freshly-loaded model is not reaped.
        self._last_used_at: float | None = None

    def _ensure_loaded(self) -> None:
        """Load the MLX model on first use. Idempotent: skips if already loaded.

        Raises:
            ImportError: If mlx-lm is not installed.
            RuntimeError: If the model cannot be loaded or probe fails.
        """
        if self._model is not None:
            return

        try:
            from mlx_lm import generate, load
            from mlx_lm.sample_utils import make_sampler
        except ImportError as exc:
            raise ImportError(
                "mlx-lm is required for Qwen3AbstractionProvider. Install with: pip install mlx-lm"
            ) from exc

        self._generate_fn = generate
        self._greedy_sampler = make_sampler(temp=0.0)

        logger.info("Loading abstraction model: %s", self._model_id)
        try:
            model, tokenizer = load(self._model_id)
        except Exception as exc:
            # Reset deferred state so a retry can attempt loading again
            self._generate_fn = None
            self._greedy_sampler = None
            raise RuntimeError(
                f"Failed to load abstraction model '{self._model_id}': {exc}"
            ) from exc

        # Validation probe (AD-026)
        try:
            messages = [
                {"role": "system", "content": _format_system_prompt(None)},
                {"role": "user", "content": "Test document content."},
            ]
            probe_prompt = tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            probe_result = generate(
                model,
                tokenizer,
                prompt=probe_prompt,
                max_tokens=20,
                verbose=False,
                sampler=self._greedy_sampler,
            )
            if not probe_result or not probe_result.strip():
                raise RuntimeError("Probe generation returned empty output")
        except RuntimeError:
            self._generate_fn = None
            self._greedy_sampler = None
            raise
        except Exception as exc:
            self._generate_fn = None
            self._greedy_sampler = None
            raise RuntimeError(
                f"Abstraction model probe failed for '{self._model_id}': {exc}"
            ) from exc

        # Commit loaded state only after probe succeeds
        self._model = model
        self._tokenizer = tokenizer
        logger.info("Abstraction model loaded: %s", self._model_id)

    def _build_prompt(self, text: str, doc_type: str | None) -> str:
        """Build a chat-template prompt for abstract generation."""
        messages = [
            {"role": "system", "content": _format_system_prompt(doc_type)},
            {"role": "user", "content": text},
        ]
        return self._tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )

    def _truncate_for_context(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        """Truncate document text to fit within context window (AD-031).

        Preserves leading content (title, abstract, introduction) by
        truncating from the end. Returns the original text if it fits.
        """
        # Measure template overhead with empty user content
        overhead_prompt = self._build_prompt("", doc_type)
        overhead_tokens = len(self._tokenizer.encode(overhead_prompt))

        available = self._context_window - max_tokens - overhead_tokens
        if available <= 0:
            # Extreme case: just use a minimal slice
            available = 100

        text_tokens = self._tokenizer.encode(text)
        if len(text_tokens) <= available:
            return text

        logger.info(
            "Truncating input from %d to %d tokens (context_window=%d, max_tokens=%d, overhead=%d)",
            len(text_tokens),
            available,
            self._context_window,
            max_tokens,
            overhead_tokens,
        )
        truncated_tokens = text_tokens[:available]
        return self._tokenizer.decode(truncated_tokens)

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        """Generate a semantic abstract from document text.

        On first call, loads the MLX model and validates with a probe
        generation. Subsequent calls reuse the loaded model.

        Args:
            text: Full document text from the projection stage.
            max_tokens: Upper bound on abstract length in tokens.
            doc_type: The document's type, surfaced to the model so it
                can choose appropriate descriptive verbs (prescribes,
                argues, narrates, defines). May be None.

        Returns:
            Non-empty abstract string (AD-027).

        Raises:
            RuntimeError: If text is empty, model fails to load, or
                model produces empty output.
        """
        # Edge guard (AD-027, AD-030)
        if not text or not text.strip():
            raise RuntimeError("Cannot generate abstract from empty document text")

        async with _generation_lock:
            # Preflight unified-memory check (guardrail 1).
            # Surfaces a structured error to the MCP caller in place of
            # an MLX-side process abort or kernel panic (F8). Runs on the
            # event loop before dispatch -- it is a cheap syscall and must
            # gate the offloaded work.
            free = free_unified_memory_bytes()
            threshold = min_free_bytes()
            if free < threshold:
                raise UnifiedMemoryExhaustedError(
                    free_bytes=free,
                    min_free_bytes=threshold,
                    model_id=self._model_id,
                )

            # Run the blocking model load + inference on a dedicated
            # single-thread executor so the multi-second MLX generation and
            # the first-call model load never freeze the lone event loop.
            # The single worker thread keeps every model operation on one
            # thread and, with _generation_lock, preserves the
            # one-inference-at-a-time guarantee -- no second MLX context, so
            # the resident-memory budget is untouched. The lock is held
            # across the await, so the executor never queues more than one
            # generation.
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="sage-abstraction"
                )
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                self._executor, self._generate_sync, text, max_tokens, doc_type
            )

        # Post-process (AD-027)
        abstract = result.strip() if result else ""
        if not abstract:
            raise RuntimeError(
                f"Abstraction model returned empty output for {len(text)} chars of input"
            )

        # Publish idle tracker for the eviction policy. Set
        # only on a successful generation so a failed call does not
        # extend the "last used" window.
        self._last_used_at = time.monotonic()

        return abstract

    def _generate_sync(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        """Blocking model load + inference. Runs on the dedicated executor.

        Lazily loads the MLX model on first use (AD-026 revised, AD-035),
        truncates the input to the context window (AD-031), builds the
        chat-template prompt (AD-028), and invokes the model with greedy
        decoding (AD-029). Errors from the model propagate naturally
        (AD-033). Always invoked through the single-thread executor so the
        ~16 GB load and the multi-second generation stay off the loop.
        """
        self._ensure_loaded()
        truncated_text = self._truncate_for_context(text, max_tokens, doc_type)
        prompt = self._build_prompt(truncated_text, doc_type)
        return self._generate_fn(
            self._model,
            self._tokenizer,
            prompt=prompt,
            max_tokens=max_tokens,
            verbose=False,
            sampler=self._greedy_sampler,
        )

    # ── Idle-driven eviction primitive ───────────────────────
    #
    # Prevention half of the F8 GPU OOM pattern. added the
    # reactive half (preflight raise + single-flight lock); without
    # an eviction path, the resident ~16 GB Qwen3 footprint sits in
    # unified memory until process exit, regardless of idle time.
    #
    # Pattern options weighed (acceptance criteria):
    # - LFU/LRU eviction of model contexts — CHOSEN. CAS holds one
    # resident MLX model, so this reduces to "unload the one
    # model when it has been idle longer than the threshold."
    # - Watchdog process monitoring resident memory — rejected.
    # Adds a second long-lived component (lifecycle, supervisor,
    # IPC) disproportionate to the single-developer Mac setup.
    # - Graceful degradation (smaller model, batched generation) —
    # rejected. Changes output characteristics; different concern.
    # - Hybrid — already achieved: (reactive) +
    # (preventive) compose. Nothing is replaced.
    #
    # Residual: caller-side wiring (a supervisor, a periodic task,
    # an external signal) is deliberately out of scope. This module
    # exposes the primitive; the policy is driven by whoever calls
    # evict_if_idle, on whatever cadence makes sense for them.
    #
    # No ADR: per CAS ADR Authoring Conventions, idle-eviction of a
    # large in-memory resource is generic performance hygiene that
    # applies equally well to any Python project — it does not
    # capture a CAS-specific architectural commitment. Rationale
    # lands here in the code, where the future reader most needs it.

    async def unload(self) -> bool:
        """Release the resident MLX model and tokenizer.

        Acquires the module-level ``_generation_lock`` so the unload
        cannot race with an in-flight ``generate_abstract``. Idempotent:
        returns ``False`` (no-op) when no model is currently loaded;
        returns ``True`` after successfully clearing the deferred state.
        After unload, the next ``generate_abstract`` call re-fires the
        existing lazy-load path in ``_ensure_loaded``.
        """
        async with _generation_lock:
            was_loaded = self._model is not None

            self._model = None
            self._tokenizer = None
            self._generate_fn = None
            self._greedy_sampler = None
            self._last_used_at = None

            # Release the dedicated inference thread alongside the model so
            # an evicted (or never-completed) provider holds no resident
            # worker. The next generate_abstract re-creates both via the
            # lazy paths. Done before the early return so a provider whose
            # first model load failed still sheds its executor.
            if self._executor is not None:
                self._executor.shutdown(wait=True)
                self._executor = None

            if not was_loaded:
                return False

            # Best-effort: clear the MLX/Metal command-buffer cache so
            # the freed memory is actually returned to unified memory
            # rather than held by the framework. Wrapped in try/except
            # because mlx may not be installed in some environments
            # (tests with SAGE_TEST_STUB_PROVIDERS=1).
            try:
                import mlx.core as mx

                mx.metal.clear_cache()
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                logger.debug("mlx.metal.clear_cache() unavailable: %s", exc)

            logger.info("Abstraction model unloaded: %s", self._model_id)
            return True

    async def evict_if_idle(self, idle_threshold_seconds: float) -> bool:
        """Unload the model iff it has been idle longer than the threshold.

        Returns ``True`` if eviction occurred, ``False`` otherwise. The
        method is safe to call regardless of load state and regardless
        of whether ``generate_abstract`` has ever been invoked:

          * Model not loaded → ``False``, no work.
          * Model loaded but never used (``_last_used_at is None``) →
            ``False``. A freshly-loaded model is not idle.
          * Model loaded, used recently → ``False``.
          * Model loaded, idle longer than threshold → unload, return
            whatever ``unload()`` returned.
        """
        if self._model is None:
            return False
        if self._last_used_at is None:
            return False
        if time.monotonic() - self._last_used_at < idle_threshold_seconds:
            return False
        return await self.unload()


# ── Process-level singleton ─────────────────────────────────
#
# initialize_services() in sage/mcp_init.py used to construct a fresh
# Qwen3AbstractionProvider per vault. __init__ is cheap, but each instance
# triggers its own MLX model load (~16 GB) on first generate_abstract,
# wasting unified memory and producing per-vault huggingface dumps.
# Qwen3 is SAGE-stack-wide; a single process-level instance suffices.

_singleton: Qwen3AbstractionProvider | None = None


def get_qwen3_abstraction_provider(
    model_id: str,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
) -> Qwen3AbstractionProvider:
    """Return the process-wide Qwen3AbstractionProvider, constructing on first call.

    Subsequent calls return the cached instance. Requesting a different
    ``model_id`` than the cached instance raises ``RuntimeError`` rather
    than silently loading a second MLX model — vaults are expected to
    agree on the abstraction model per CAS design.
    """
    global _singleton
    if _singleton is None:
        _singleton = Qwen3AbstractionProvider(model_id=model_id, context_window=context_window)
    elif _singleton._model_id != model_id:
        raise RuntimeError(
            f"Qwen3AbstractionProvider singleton already initialized with "
            f"model_id={_singleton._model_id!r}; cannot satisfy request "
            f"for model_id={model_id!r}. Qwen3 is intended to be "
            f"SAGE-stack-wide; reconcile vault configs or call "
            f"_reset_qwen3_singleton() if intentional."
        )
    return _singleton


def _reset_qwen3_singleton() -> None:
    """Clear the cached provider. For test isolation only."""
    global _singleton
    _singleton = None
