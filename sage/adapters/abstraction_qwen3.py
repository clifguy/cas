"""Qwen3 AbstractionProvider implementation via MLX.

Uses mlx-lm to load Qwen3-30B-A3B-Instruct-2507 (or compatible model)
and generate density-proportional semantic abstracts on Apple Silicon.
"""

import logging

from sage.adapters.interfaces import AbstractionProvider

logger = logging.getLogger(__name__)

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

        return abstract
