"""Qwen3 AbstractionProvider implementation via MLX.

Uses mlx-lm to load Qwen3-30B-A3B-Instruct-2507 (or compatible model)
and generate density-proportional semantic abstracts on Apple Silicon.
"""

import asyncio
import logging
import time

from sage.adapters.interfaces import AbstractionProvider
from sage.utils.unified_memory import (
    UnifiedMemoryExhaustedError,
    free_unified_memory_bytes,
    min_free_bytes,
)

logger = logging.getLogger(__name__)

# Module-level lock serializing all MLX generate_abstract calls (T-0029
# guardrail 2). Two concurrent ingest pipelines previously could both
# drive Qwen3 toward the unified-memory ceiling at once; this lock
# enforces single-flight discipline.
_generation_lock = asyncio.Lock()

SYSTEM_PROMPT_TEMPLATE = (
    "You are producing a relevance-triage card for an autonomous agent that "
    "has discovered this document via search. The agent will read this card "
    "to decide whether to fetch the full document. Write a description of "
    'the document in third person ("This document...", "The guideline...", '
    '"The text..."). Cover what the document is about, what it claims or '
    "prescribes, what topics it covers, and where relevant what it does not "
    "cover.\n"
    "\n"
    "Do not write in the voice, style, or genre the document discusses -- "
    "describe the document, do not produce a specimen of it. Do not introduce "
    "specifics (numbers, names, dates, quotes, examples) that are not present "
    "in the source text. The document's title{doc_type_clause}, tags, and "
    "project are already visible to the agent; do not restate them. Length "
    "should be proportional to the document's complexity: longer for dense "
    "or multi-topic documents, shorter for simple or narrowly-scoped ones. "
    "Output only the description, with no preamble, labels, or commentary."
)


def _format_system_prompt(doc_type: str | None) -> str:
    """Render the system prompt with optional doc_type substitution."""
    if doc_type:
        clause = f', type ("{doc_type}")'
    else:
        clause = ""
    return SYSTEM_PROMPT_TEMPLATE.format(doc_type_clause=clause)


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

        # Idle tracker for the T-0068 eviction policy. Updated at the
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
            # Preflight unified-memory check (T-0029 guardrail 1).
            # Surfaces a structured error to the MCP caller in place of
            # an MLX-side process abort or kernel panic (F8).
            free = free_unified_memory_bytes()
            threshold = min_free_bytes()
            if free < threshold:
                raise UnifiedMemoryExhaustedError(
                    free_bytes=free,
                    min_free_bytes=threshold,
                    model_id=self._model_id,
                )

            # Lazy model load (AD-026 revised, AD-035)
            self._ensure_loaded()

            # Truncate if needed (AD-031)
            truncated_text = self._truncate_for_context(text, max_tokens, doc_type)

            # Build prompt and generate (AD-028, AD-029)
            prompt = self._build_prompt(truncated_text, doc_type)
            result = self._generate_fn(
                self._model,
                self._tokenizer,
                prompt=prompt,
                max_tokens=max_tokens,
                verbose=False,
                sampler=self._greedy_sampler,
            )
            # Errors from _generate_fn propagate naturally (AD-033)

        # Post-process (AD-027)
        abstract = result.strip() if result else ""
        if not abstract:
            raise RuntimeError(
                f"Abstraction model returned empty output for {len(text)} chars of input"
            )

        # Publish idle tracker for the T-0068 eviction policy. Set
        # only on a successful generation so a failed call does not
        # extend the "last used" window.
        self._last_used_at = time.monotonic()

        return abstract

    # ── T-0068: idle-driven eviction primitive ───────────────────────
    #
    # Prevention half of the F8 GPU OOM pattern. T-0029 added the
    # reactive half (preflight raise + single-flight lock); without
    # an eviction path, the resident ~16 GB Qwen3 footprint sits in
    # unified memory until process exit, regardless of idle time.
    #
    # Pattern options weighed (T-0068 acceptance criteria):
    #   - LFU/LRU eviction of model contexts — CHOSEN. CAS holds one
    #     resident MLX model, so this reduces to "unload the one
    #     model when it has been idle longer than the threshold."
    #   - Watchdog process monitoring resident memory — rejected.
    #     Adds a second long-lived component (lifecycle, supervisor,
    #     IPC) disproportionate to the single-developer Mac setup.
    #   - Graceful degradation (smaller model, batched generation) —
    #     rejected. Changes output characteristics; different concern.
    #   - Hybrid — already achieved: T-0029 (reactive) + T-0068
    #     (preventive) compose. Nothing is replaced.
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
            if self._model is None:
                return False

            self._model = None
            self._tokenizer = None
            self._generate_fn = None
            self._greedy_sampler = None
            self._last_used_at = None

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


# ── Process-level singleton (T-0060) ─────────────────────────────────
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
