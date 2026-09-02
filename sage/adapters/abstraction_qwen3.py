"""Qwen3 AbstractionProvider implementation via MLX.

Uses mlx-lm to load the configured Qwen3-family model and generate
density-proportional semantic abstracts on Apple Silicon.
"""

import asyncio
import json
import logging
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, NamedTuple

from sage.adapters.abstraction_prompt import (
    SYSTEM_PROMPT_TEMPLATE,  # noqa: F401 -- re-exported for callers importing from this module
    _format_system_prompt,
    wrap_source_document,
)
from sage.adapters.abstraction_utils import (
    _OPENER_EDGE_PUNCTUATION,
    forbidden_opener_continuations,
    opener_sentence_terminated,
)
from sage.adapters.interfaces import AbstractionMemoryExhaustedError, AbstractionProvider
from sage.utils.unified_memory import (
    UnifiedMemoryExhaustedError,
    free_unified_memory_bytes,
    min_free_bytes,
)

logger = logging.getLogger(__name__)

# Structured latency records land on a dedicated logger, mirroring the
# sage.{storage,content,retrieval}.timing family: one JSON payload per record
# as the log message, so a single grep/jq pipeline can join across emitters.
# The abstraction path emits at two levels -- a provider-neutral record from
# the ingestion service, and this provider's implementation-specific
# breakdown -- because the providers share no vocabulary below wall-clock
# time: prefill and decode are MLX concepts with no counterpart in a hosted
# API call.
timing_logger = logging.getLogger("sage.abstraction.timing")

# Module-level lock serializing all MLX generate_abstract calls (
# guardrail 2). Two concurrent ingest pipelines previously could both
# drive Qwen3 toward the unified-memory ceiling at once; this lock
# enforces single-flight discipline. It is scoped to this local provider:
# hosted providers carry no unified-memory budget and must not serialize
# their network calls behind it.
_generation_lock = asyncio.Lock()


# Prompt window an *unconfigured* stack runs on. Deliberately a bounded floor
# rather than a reading of the loaded model: the two are not the same choice,
# and the difference matters most for the models where it is least visible.
#
# Deferring to the model's native window would make an unconfigured stack
# inherit whatever the weights advertise, which for a long-context family runs
# an order of magnitude above this number. The window sets a truncation
# threshold, so nothing changes for a document that already fits -- but a
# document that does not now prefills the whole way, and at those lengths a
# single abstract costs minutes of prefill and a unified-memory spike large
# enough to matter on a workstation. A stack nobody configured is exactly the
# one that should not opt into that silently.
#
# So the native window stays reachable, but only by writing it down: set
# `abstraction.context_window` in the stack config (CAS-ADR-030), where the
# value is visible, reviewable, and clamped to the native window at model load
# if it overshoots. Raising this constant to track a particular model would
# move that decision back out of sight, which is how the gap between this
# number and the configured model's capacity went unnoticed for as long as it
# did.
DEFAULT_CONTEXT_WINDOW = 32768


class TruncationOutcome(NamedTuple):
    """Result of fitting document text to the available prompt window.

    ``input_tokens`` is the measured token length of the *original* text, and
    is None when the cheap pre-check established the text could not overflow
    the window and the encode was skipped. A caller that needs the count in
    that case can derive it from the prompt length the model reports, since
    nothing was dropped.
    """

    text: str
    input_tokens: int | None


# The smallest input slice worth sending when the output budget and template
# overhead have already claimed the whole window. A non-positive budget is not
# a usable answer -- it reads either as "everything fits" or as "send nothing"
# depending on which comparison consumes it -- so the degenerate case resolves
# to a token count that is small but real.
MINIMUM_INPUT_TOKENS = 100


def available_input_tokens(effective_window: int, max_tokens: int, overhead_tokens: int) -> int:
    """Tokens of document text that fit alongside the output and the template.

    The prompt window is shared three ways: the generated abstract reserves
    ``max_tokens``, the chat template and system prompt cost
    ``overhead_tokens``, and whatever remains is what the document itself may
    spend. A document longer than the remainder is truncated before the model
    sees it.

    Module-level and free of provider state so that a caller measuring a
    corpus offline computes the same threshold this provider enforces,
    without loading model weights to ask. Keeping that arithmetic in one
    place is the point: a second copy would agree today and diverge silently
    later, and a survey run against a drifted threshold reports the wrong set
    of documents with no signal that it did.

    Scoped to this provider. The hosted provider reserves its own input
    budget on a different basis -- charging the system prompt at its byte
    length to avoid a tokenizer round trip, and holding back a safety margin
    against a ceiling it cannot measure locally -- so the two are
    deliberately separate rather than a duplication waiting to be collapsed.

    Args:
        effective_window: The window actually in force -- the smaller of the
            configured value and the loaded model's native prompt length.
        max_tokens: Output budget reserved for the generated abstract.
        overhead_tokens: Chat-template and system-prompt cost for the
            document type in question.

    Returns:
        The input budget in tokens, never below ``MINIMUM_INPUT_TOKENS``.
    """
    available = effective_window - max_tokens - overhead_tokens
    if available <= 0:
        return MINIMUM_INPUT_TOKENS
    return available


def _phase_duration_ms(tokens: int | None, tokens_per_second: float | None) -> float | None:
    """Convert a token count and a rate into a duration in milliseconds.

    Returns None when either input is missing or the rate is zero, so a
    degenerate measurement is reported as absent rather than as a duration
    that happens to be zero or infinite.
    """
    if not tokens or not tokens_per_second:
        return None
    return tokens / tokens_per_second * 1000.0


def _advertised_context_window(args: object) -> int | None:
    """Read a positive ``max_position_embeddings`` off a model-args object."""
    native = getattr(args, "max_position_embeddings", None)
    return native if isinstance(native, int) and native > 0 else None


def _resolve_native_context_window(model: object) -> int | None:
    """Read the loaded model's native prompt length, or None if unadvertised.

    Best-effort by design. The attribute is conventional across the model
    families this provider loads but is not part of any contract, so a model
    that omits it yields None -- an unknown native window leaves the
    configured value standing rather than becoming a load failure.

    Hybrid-attention families wrap a nested text model, leaving the outer
    args carrying only the model type and a nested text config while the
    prompt length sits on the inner model. Those are read second, so a model
    that advertises at the top level keeps that value: the nested read is a
    fallback for a shape that would otherwise resolve to None, not a
    competing source. Resolving None for a model that does advertise a window
    is the damaging outcome, since it lets an oversized configured window
    stand unclamped and degrade the output with no signal anywhere.
    """
    native = _advertised_context_window(getattr(model, "args", None))
    if native is not None:
        return native

    inner = getattr(model, "language_model", None)
    return _advertised_context_window(getattr(inner, "args", None))


# Wordings the Metal allocator uses when unified memory runs out mid-inference.
# Matched together with a "metal" mention so that only allocator exhaustion is
# reclassified; any other generation failure keeps its retry budget.
_MEMORY_EXHAUSTION_MARKERS = (
    "out of memory",
    "insufficient memory",
    "maximum allowed buffer size",
)


def _is_memory_exhaustion(exc: BaseException) -> bool:
    """Whether a generation failure reports accelerator-memory exhaustion.

    Narrow by construction: a plain ``MemoryError``, or an exception whose
    message carries both a Metal mention and an exhaustion wording. Everything
    else -- other bad states, other RuntimeErrors -- stays unclassified so the
    retry budget above the port keeps covering failures a later attempt could
    resolve.
    """
    if isinstance(exc, MemoryError):
        return True
    message = str(exc).lower()
    return "metal" in message and any(marker in message for marker in _MEMORY_EXHAUSTION_MARKERS)


# ---------------------------------------------------------------------------
# Type-restating-opener constraint (CAS-ADR-020 clause (f))
# ---------------------------------------------------------------------------


def _normalize_surface(text: str) -> str:
    """Index key for a token's decoded surface.

    Mirrors what the detector's tokenizer does to a word of the opener --
    NFKC, edge punctuation stripped, casefolded -- with one addition: a
    leading space is preserved as a single space, because it is what
    separates a token that opens a word from one that continues it. The
    constraint names both, and masking one for the other would change
    generation at a position the detector never inspects.

    Returns the empty string for a surface that carries no word, which
    the caller drops.
    """
    normalized = unicodedata.normalize("NFKC", text)
    leading = " " if normalized[:1].isspace() else ""
    body = normalized.strip().strip(_OPENER_EDGE_PUNCTUATION)
    return f"{leading}{body.casefold()}" if body else ""


def _build_surface_index(tokenizer: Any) -> dict[str, tuple[int, ...]]:
    """Map each normalized surface in the vocabulary to the ids rendering it.

    One pass over the vocabulary, so the per-token work during generation
    is a dict lookup rather than a search. Built lazily on the first
    constrained call and released with the tokenizer it was read from: the
    ids are meaningless against a different vocabulary, and a stale index
    would mask arbitrary tokens rather than fail.

    Ids are grouped rather than overwritten because several surfaces
    normalize together -- case variants, and the punctuation-adjacent
    forms the detector compares bare.
    """
    vocab_size = getattr(tokenizer, "vocab_size", None) or len(tokenizer)
    decoded = tokenizer.batch_decode([[token_id] for token_id in range(vocab_size)])

    index: dict[str, list[int]] = {}
    for token_id, surface in enumerate(decoded):
        key = _normalize_surface(surface)
        if key:
            index.setdefault(key, []).append(token_id)
    return {key: tuple(ids) for key, ids in index.items()}


def _suppress_logits(logits: Any, token_ids: tuple[int, ...]) -> Any:
    """Drive *token_ids* out of contention in *logits*.

    Additive rather than assigned, mirroring how mlx-lm's own logit-bias
    processor writes to logits. The MLX import is local because it happens
    only inside a live generation: constructing a constraint, and every
    test that drives one, stays free of the accelerator runtime.
    """
    import mlx.core as mx

    floor = mx.full((len(token_ids),), mx.finfo(logits.dtype).min, dtype=logits.dtype)
    return logits.at[:, mx.array(token_ids)].add(floor)


class _OpenerConstraint:
    """Suppress the tokens that would complete a type-restating opener.

    CAS-ADR-020 clause (f) instructs the prompt against restating the type
    the discovering agent already sees as metadata. The post-generation
    check reports the breach and the bounded excision repairs the one
    shape whose relative clause can be promoted to the main predicate;
    the rest were measured unrepairable, because reaching a finite verb
    from a participial or prepositional modifier means composing one.
    This closes those at the only point where nothing has to be composed:
    before the type word is emitted.

    Applied during decoding, so it composes with greedy sampling instead
    of competing with it. Every position the automaton does not name is
    left at argmax, and the extent of what it names is the detector's,
    which is what lets the same detector measure whether this worked.

    Stateful for the length of one generation and no longer: the sole
    state is the prompt watermark and the closed flag, and a reused
    instance would carry a stale watermark into the next call and decode
    the wrong span. The provider constructs one per generation.

    Args:
        doc_type: The document's type as supplied to the prompt.
        decode: Renders a token id sequence as text.
        surfaces: Normalized surface to the token ids rendering it.
        mask: Drives the named ids out of contention in the logits.
    """

    def __init__(
        self,
        doc_type: str | None,
        *,
        decode: Callable[[Sequence[int]], str],
        surfaces: Mapping[str, tuple[int, ...]],
        mask: Callable[[Any, tuple[int, ...]], Any],
    ) -> None:
        self._doc_type = doc_type
        self._decode = decode
        self._surfaces = surfaces
        self._mask = mask
        self._prompt_tokens: int | None = None
        self._closed = doc_type is None

    def __call__(self, tokens: Any, logits: Any) -> Any:
        if self._closed:
            return logits

        ids = tokens.tolist() if hasattr(tokens, "tolist") else list(tokens)
        if self._prompt_tokens is None:
            # MLX runs a processor only from the sampling step, never from
            # the chunked prefill loop, so this call precedes the first
            # generated token and whatever it carries is prompt.
            self._prompt_tokens = len(ids)

        generated = self._decode(ids[self._prompt_tokens :])

        # The detector reads only the opening sentence, so once that
        # sentence closes the constraint has nothing left to prevent.
        # Latched rather than re-derived because the rest of a generation
        # is most of it, and a decode per token there would buy nothing.
        if opener_sentence_terminated(generated):
            self._closed = True
            return logits

        forbidden = forbidden_opener_continuations(generated, self._doc_type)
        if not forbidden:
            return logits

        blocked = sorted(
            {token_id for surface in forbidden for token_id in self._surfaces.get(surface, ())}
        )
        if not blocked:
            return logits
        return self._mask(logits, tuple(blocked))


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
        context_window: int | None = None,
        opener_constraint: bool = False,
    ) -> None:
        self._model_id = model_id
        # Whether generation is constrained against CAS-ADR-020 clause (f)
        # openers. A decoding-time property, so it is available only here:
        # a hosted provider exposes no sampling loop to constrain, and its
        # coverage of the clause stays the prompt directive plus the
        # post-generation check.
        self._opener_constraint = opener_constraint
        # None is the "unconfigured" sentinel and resolves to the module
        # default here, at the single point that owns it. Callers forward
        # whatever their configuration carries without having to know the
        # fallback, so an unset configuration reproduces the default
        # behavior exactly.
        self._context_window = DEFAULT_CONTEXT_WINDOW if context_window is None else context_window

        # Native prompt length of the loaded weights, read once at load
        # from the model's own arguments. None means the model family does
        # not advertise one, in which case the configured window stands.
        self._native_context_window: int | None = None

        # Deferred state: populated by _ensure_loaded() on first use
        self._model = None
        self._tokenizer = None
        self._generate_fn = None
        self._greedy_sampler = None

        # Chat-template overhead in tokens, keyed by doc_type. The template
        # is constant for a given doc_type, so measuring it once per type
        # spares an encode on every subsequent call. Keyed rather than
        # single-valued because the system prompt varies by doc_type, and
        # cleared on unload because a reload may bring a different tokenizer.
        self._overhead_tokens: dict[str | None, int] = {}

        # Vocabulary surface index for the opener constraint, built on the
        # first constrained generation. Keyed to the loaded tokenizer and
        # cleared with it: token ids carry no meaning against a different
        # vocabulary, and a retained index would mask arbitrary tokens
        # rather than fail.
        self._surface_index: dict[str, tuple[int, ...]] | None = None

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
            from mlx_lm import load, stream_generate
            from mlx_lm.sample_utils import make_sampler
        except ImportError as exc:
            raise ImportError(
                "mlx-lm is required for Qwen3AbstractionProvider. Install with: pip install mlx-lm"
            ) from exc

        # The streaming entry point rather than the one-shot wrapper: the
        # wrapper is exactly this generator with its segments concatenated,
        # so the generated text is identical, but only the stream surfaces
        # the per-phase token counts and rates the latency record reports.
        self._generate_fn = stream_generate
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
            probe_result = "".join(
                response.text
                for response in stream_generate(
                    model,
                    tokenizer,
                    prompt=probe_prompt,
                    max_tokens=20,
                    sampler=self._greedy_sampler,
                )
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
        self._native_context_window = _resolve_native_context_window(model)
        if (
            self._native_context_window is not None
            and self._context_window > self._native_context_window
        ):
            # Report rather than fail: the configured window is reachable by
            # clamping, and a stack whose only defect is an over-large number
            # should still serve. What must not happen is the silent case --
            # attending past what the weights support degrades the output
            # with no signal anywhere.
            logger.warning(
                "Configured context_window %d exceeds the native window of "
                "'%s' (%d); clamping to %d",
                self._context_window,
                self._model_id,
                self._native_context_window,
                self._native_context_window,
            )
        logger.info("Abstraction model loaded: %s", self._model_id)

    def _effective_context_window(self) -> int:
        """Prompt window actually spent, in tokens.

        The smaller of the configured window and the loaded model's native
        window. Before the model loads -- and for a model family that does
        not advertise a native window -- the configured value stands.
        """
        if self._native_context_window is None:
            return self._context_window
        return min(self._context_window, self._native_context_window)

    def _build_prompt(self, text: str, doc_type: str | None) -> str:
        """Build a chat-template prompt for abstract generation."""
        messages = [
            {"role": "system", "content": _format_system_prompt(doc_type)},
            {"role": "user", "content": wrap_source_document(text)},
        ]
        return self._tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )

    def _template_overhead_tokens(self, doc_type: str | None) -> int:
        """Token cost of the chat template and system prompt for *doc_type*.

        Constant for a given doc_type and tokenizer, so it is measured once
        and cached. The cache is cleared on unload, where a subsequent reload
        may install a different tokenizer.
        """
        cached = self._overhead_tokens.get(doc_type)
        if cached is not None:
            return cached
        overhead = len(self._tokenizer.encode(self._build_prompt("", doc_type)))
        self._overhead_tokens[doc_type] = overhead
        return overhead

    def _truncate_for_context(
        self, text: str, max_tokens: int, doc_type: str | None
    ) -> TruncationOutcome:
        """Truncate document text to fit within the context window (AD-031).

        Preserves leading content (title, abstract, introduction) by
        truncating from the end. Text that already fits is returned unchanged.

        Documents short enough that they cannot possibly overflow the window
        skip the encode entirely: every token consumes at least one UTF-8
        byte, so a text whose byte length is within the available budget is
        within it in tokens too. The bound is one-directional -- it can only
        prove a document safe, never oversized -- so anything it does not
        clear falls through to the exact measurement below.
        """
        overhead_tokens = self._template_overhead_tokens(doc_type)

        available = available_input_tokens(
            self._effective_context_window(), max_tokens, overhead_tokens
        )

        if len(text.encode("utf-8")) <= available:
            return TruncationOutcome(text=text, input_tokens=None)

        text_tokens = self._tokenizer.encode(text)
        if len(text_tokens) <= available:
            return TruncationOutcome(text=text, input_tokens=len(text_tokens))

        logger.info(
            "Truncating input from %d to %d tokens (context_window=%d, max_tokens=%d, overhead=%d)",
            len(text_tokens),
            available,
            self._effective_context_window(),
            max_tokens,
            overhead_tokens,
        )
        truncated_tokens = text_tokens[:available]
        return TruncationOutcome(
            text=self._tokenizer.decode(truncated_tokens),
            input_tokens=len(text_tokens),
        )

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
            AbstractionMemoryExhaustedError: If accelerator memory is
                exhausted mid-inference. Non-retryable: repeating the
                attempt re-pays the full prefill for the same failure.
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
            try:
                result = await loop.run_in_executor(
                    self._executor, self._generate_sync, text, max_tokens, doc_type
                )
            except Exception as exc:
                if _is_memory_exhaustion(exc):
                    # Deterministic at this window: the document, the model,
                    # and the memory budget are identical on every attempt, so
                    # a retry above the port re-pays the full prefill only to
                    # hit the same allocation failure. Reclassified here so the
                    # caller's retry budget is spent only on failures a later
                    # attempt could resolve. The preflight check above is the
                    # deliberate contrast: it costs one syscall to repeat and
                    # may clear as other load subsides, so it stays retryable.
                    raise AbstractionMemoryExhaustedError(self._model_id, len(text)) from exc
                raise

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

    def _build_opener_constraint(self, doc_type: str | None) -> "_OpenerConstraint | None":
        """A fresh clause (f) constraint for one generation, or None.

        None when the stack has not enabled the constraint, and when the
        document carries no type -- there is then no phrase to restate,
        and an inert processor would cost a decode per token for nothing.

        Fresh per call because the constraint carries per-generation state;
        reusing one would carry a stale prompt watermark into the next
        generation, and greedy decoding would stop being reproducible.
        """
        if not self._opener_constraint or doc_type is None:
            return None
        if self._surface_index is None:
            self._surface_index = _build_surface_index(self._tokenizer)
        return _OpenerConstraint(
            doc_type,
            decode=self._tokenizer.decode,
            surfaces=self._surface_index,
            mask=_suppress_logits,
        )

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
        outcome = self._truncate_for_context(text, max_tokens, doc_type)
        prompt = self._build_prompt(outcome.text, doc_type)

        constraint = self._build_opener_constraint(doc_type)
        result = ""
        final = None
        for response in self._generate_fn(
            self._model,
            self._tokenizer,
            prompt=prompt,
            max_tokens=max_tokens,
            sampler=self._greedy_sampler,
            **({"logits_processors": [constraint]} if constraint is not None else {}),
        ):
            result += response.text
            final = response

        self._emit_latency_record(text, outcome, doc_type, final)
        return result

    def _emit_latency_record(
        self,
        text: str,
        outcome: TruncationOutcome,
        doc_type: str | None,
        final: object | None,
    ) -> None:
        """Log one structured record describing the generation just completed.

        Measurement only: every value is read off the generation the model
        already performed, so the record cannot influence what was produced.

        The record pairs with the provider-neutral record the ingestion
        service emits for the same call -- both carry ``document_chars`` --
        and with the truncation notice above, whose two token counts are this
        record's ``input_tokens`` and ``retained_tokens``.

        ``document_chars`` measures the text handed to this provider, not
        whatever reached the model after fitting to the context window.
        ``prompt_tokens`` is the model's own prompt length, so the constant
        template overhead is its difference from ``retained_tokens`` -- a
        figure the record yields on every generation, not only on one that
        happened to truncate.
        """
        if final is None:
            return

        prompt_tokens = getattr(final, "prompt_tokens", None)
        overhead_tokens = self._overhead_tokens.get(doc_type)
        retained_tokens = None
        if prompt_tokens is not None and overhead_tokens is not None:
            retained_tokens = max(prompt_tokens - overhead_tokens, 0)

        # Nothing was dropped when the truncation helper skipped the encode,
        # so the retained count is also the input count -- exact, not an
        # estimate, and free where measuring the input would not have been.
        input_tokens = outcome.input_tokens
        if input_tokens is None:
            input_tokens = retained_tokens

        # Each rate is the model's own reported figure rather than a ratio
        # recomputed from the count and duration beside it. The two agree
        # arithmetically on an ordinary generation -- the durations are
        # derived from these very rates -- but only the reported figure
        # survives a phase whose duration is unmeasurable.
        prompt_tps = getattr(final, "prompt_tps", None)
        generation_tokens = getattr(final, "generation_tokens", None)
        generation_tps = getattr(final, "generation_tps", None)

        payload: dict[str, object] = {
            "layer": "abstraction",
            "label": "abstract.mlx",
            "model": self._model_id,
            "document_chars": len(text),
            "input_tokens": input_tokens,
            "retained_tokens": retained_tokens,
            # The same value retained_tokens was derived from, so the two
            # cannot disagree about the template overhead between them.
            "prompt_tokens": prompt_tokens,
            "generated_tokens": generation_tokens,
            "prefill_ms": _phase_duration_ms(prompt_tokens, prompt_tps),
            "prefill_tps": prompt_tps,
            "decode_ms": _phase_duration_ms(generation_tokens, generation_tps),
            "decode_tps": generation_tps,
        }
        timing_logger.info(json.dumps(payload))

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

            # Both are properties of the weights and tokenizer just released;
            # a reload may bring different ones, and a stale overhead would
            # silently mis-size every subsequent prompt.
            self._native_context_window = None
            self._overhead_tokens = {}
            self._surface_index = None

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
    context_window: int | None = None,
    opener_constraint: bool = False,
) -> Qwen3AbstractionProvider:
    """Return the process-wide Qwen3AbstractionProvider, constructing on first call.

    Subsequent calls return the cached instance. Requesting a different
    ``model_id`` than the cached instance raises ``RuntimeError`` rather
    than silently loading a second MLX model — vaults are expected to
    agree on the abstraction model per CAS design.
    """
    global _singleton
    if _singleton is None:
        _singleton = Qwen3AbstractionProvider(
            model_id=model_id,
            context_window=context_window,
            opener_constraint=opener_constraint,
        )
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
