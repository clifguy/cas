"""Qwen3 AbstractionProvider implementation via MLX.

Uses mlx-lm to load Qwen3-30B-A3B-Instruct-2507 (or compatible model)
and generate density-proportional semantic abstracts on Apple Silicon.
"""

import logging

from sage.adapters.interfaces import AbstractionProvider

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a technical abstraction engine. Given the full text of a document, "
    "produce a concise semantic abstract that captures the key concepts, methods, "
    "and findings. The abstract should be density-proportional: longer for complex, "
    "information-rich documents; shorter for simple ones. Output only the abstract "
    "text with no preamble, labels, or commentary."
)

DEFAULT_CONTEXT_WINDOW = 32768


class Qwen3AbstractionProvider(AbstractionProvider):
    """Production abstraction provider using Qwen3 via MLX.

    Loads the model eagerly at init and validates with a probe generation.
    Raises on model load failure (AD-026). Uses greedy decoding via
    make_sampler(temp=0) for deterministic output (AD-029). Truncates long input to fit the
    context window, preserving leading content (AD-031).
    """

    def __init__(
        self,
        model_id: str,
        context_window: int = DEFAULT_CONTEXT_WINDOW,
    ) -> None:
        try:
            from mlx_lm import load, generate
            from mlx_lm.sample_utils import make_sampler
        except ImportError as exc:
            raise ImportError(
                "mlx-lm is required for Qwen3AbstractionProvider. "
                "Install with: pip install mlx-lm"
            ) from exc

        self._generate_fn = generate
        self._greedy_sampler = make_sampler(temp=0.0)
        self._model_id = model_id
        self._context_window = context_window

        # Eager model load (AD-026)
        logger.info("Loading abstraction model: %s", model_id)
        try:
            self._model, self._tokenizer = load(model_id)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load abstraction model '{model_id}': {exc}"
            ) from exc

        # Validation probe (AD-026)
        try:
            probe_prompt = self._build_prompt("Test document content.")
            probe_result = self._generate_fn(
                self._model,
                self._tokenizer,
                prompt=probe_prompt,
                max_tokens=20,
                verbose=False,
                sampler=self._greedy_sampler,
            )
            if not probe_result or not probe_result.strip():
                raise RuntimeError("Probe generation returned empty output")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Abstraction model probe failed for '{model_id}': {exc}"
            ) from exc

        logger.info("Abstraction model loaded: %s", model_id)

    def _build_prompt(self, text: str) -> str:
        """Build a chat-template prompt for abstract generation."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        return self._tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )

    def _truncate_for_context(self, text: str, max_tokens: int) -> str:
        """Truncate document text to fit within context window (AD-031).

        Preserves leading content (title, abstract, introduction) by
        truncating from the end. Returns the original text if it fits.
        """
        # Measure template overhead with empty user content
        overhead_prompt = self._build_prompt("")
        overhead_tokens = len(self._tokenizer.encode(overhead_prompt))

        available = self._context_window - max_tokens - overhead_tokens
        if available <= 0:
            # Extreme case: just use a minimal slice
            available = 100

        text_tokens = self._tokenizer.encode(text)
        if len(text_tokens) <= available:
            return text

        logger.info(
            "Truncating input from %d to %d tokens (context_window=%d, "
            "max_tokens=%d, overhead=%d)",
            len(text_tokens), available, self._context_window,
            max_tokens, overhead_tokens,
        )
        truncated_tokens = text_tokens[:available]
        return self._tokenizer.decode(truncated_tokens)

    async def generate_abstract(self, text: str, max_tokens: int) -> str:
        """Generate a semantic abstract from document text.

        Args:
            text: Full document text from the projection stage.
            max_tokens: Upper bound on abstract length in tokens.

        Returns:
            Non-empty abstract string (AD-027).

        Raises:
            RuntimeError: If text is empty or model produces empty output.
        """
        # Edge guard (AD-027, AD-030)
        if not text or not text.strip():
            raise RuntimeError(
                "Cannot generate abstract from empty document text"
            )

        # Truncate if needed (AD-031)
        truncated_text = self._truncate_for_context(text, max_tokens)

        # Build prompt and generate (AD-028, AD-029)
        prompt = self._build_prompt(truncated_text)
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
                "Abstraction model returned empty output for "
                f"{len(text)} chars of input"
            )

        return abstract
