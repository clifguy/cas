"""Hosted AbstractionProvider implementation via the Anthropic Messages API.

Generates a density-proportional semantic abstract with a hosted Claude
model (e.g. Haiku) behind the same ``AbstractionProvider`` contract as the
local Qwen3 provider. Because the call is a network request with no
unified-memory budget, it is deliberately not serialized behind the local
provider's ``_generation_lock`` (the F-8 guard). Making abstraction
selectable to a hosted call is what lets the SAGE core run on a non-Apple
target (CAS-ADR-042).

Input that would overrun the model's context window is reduced to fit rather
than sent whole and rejected, so an over-large document degrades to a partial
abstract instead of failing ingestion outright (CAS-ADR-011). The ceiling is
read from the provider's model registry rather than held here as a per-model
table, so it tracks the model roster instead of ageing with it.
"""

import asyncio
import logging
import re

from sage.adapters.abstraction_prompt import _format_system_prompt, wrap_source_document
from sage.adapters.interfaces import AbstractionInputTooLargeError, AbstractionProvider

logger = logging.getLogger(__name__)

# Headroom reserved between the counted input and the model's stated ceiling.
# Counting and generating are separate requests, and the request envelope
# carries a little beyond the system prompt and the message content, so a
# fixed reserve keeps a count that lands exactly at the ceiling from
# overrunning it on the call that follows.
_INPUT_TOKEN_SAFETY_MARGIN = 1024

# Floor under the input budget. A budget at or below zero means the output
# budget and prompt overhead already exceed the model's input ceiling, which
# is a misconfiguration rather than an over-large document; a minimal slice
# keeps the request well-formed so the API reports the real problem.
_MIN_INPUT_BUDGET_TOKENS = 100

# Upper bound on the count-and-shrink rounds spent narrowing an over-length
# document. Each round is a proportional estimate, which overshoots only
# where token density is uneven, so a small number converges in practice; the
# byte-bound fallback guarantees termination whatever the counts come back as.
_MAX_SHRINK_ROUNDS = 3

# The API's wording for input that overran the model's ceiling. Matching the
# wording rather than the status code alone is deliberate -- see
# ``_reports_input_too_long``.
_INPUT_TOO_LONG_PATTERN = re.compile(r"\b(?:prompt|input)\s+is\s+too\s+long\b", re.IGNORECASE)


def _input_budget_tokens(limit: int, max_tokens: int, system_prompt: str) -> int:
    """Tokens available for document text under a model's input ceiling.

    The system prompt is charged at its UTF-8 byte length rather than its
    token count: a token never encodes to fewer than one byte, so the byte
    length bounds the tokens it can cost and needs no round trip to
    establish. The output budget is reserved as well, which costs at most one
    abstract's worth of a window measured in the hundreds of thousands and
    removes any dependence on whether the reported ceiling already nets it out.

    Args:
        limit: The model's maximum input size in tokens.
        max_tokens: Upper bound on the generated abstract, reserved here.
        system_prompt: The system prompt the request will carry.

    Returns:
        Token budget for the document text, at least
        ``_MIN_INPUT_BUDGET_TOKENS``.
    """
    budget = limit - max_tokens - len(system_prompt.encode("utf-8")) - _INPUT_TOKEN_SAFETY_MARGIN
    return max(budget, _MIN_INPUT_BUDGET_TOKENS)


def _reports_input_too_long(exc: BaseException) -> bool:
    """Whether an SDK error reports input that overran the model's ceiling.

    Narrow by construction. A bad-request status alone is not enough: the same
    status also covers malformed requests and unknown model ids, which a later
    attempt can resolve once the configuration is corrected, and which
    therefore keep their retry budget. Only the over-length case is
    deterministic in the document itself.
    """
    from anthropic import BadRequestError

    if not isinstance(exc, BadRequestError):
        return False
    return _INPUT_TOO_LONG_PATTERN.search(str(exc)) is not None


def _discovery_failure_is_permanent(exc: BaseException) -> bool:
    """Whether a failed ceiling lookup is settled rather than worth retrying.

    Narrow by construction, and biased toward retrying. A client-error status
    the request itself provoked -- an unknown model id, a rejected key, a
    principal without access -- reports something no later attempt resolves
    while the configuration stands, so the answer is kept. Everything else is
    treated as transient: connection failures, timeouts, throttling, server
    errors, and any exception type this classifier does not recognize. The
    unrecognized case falls to the retrying side deliberately -- a needless
    retry costs one round trip, while wrongly settling on a failure costs the
    input check for the life of the process.
    """
    from anthropic import APIStatusError

    if not isinstance(exc, APIStatusError):
        return False
    status = exc.status_code
    # 408 and 409 sit in the client-error range but describe a moment rather
    # than the request; 429 says to come back later in as many words.
    return 400 <= status < 500 and status not in (408, 409, 429)


class AnthropicAbstractionProvider(AbstractionProvider):
    """Abstraction provider backed by a hosted Claude model.

    Construction stores configuration only; the SDK client is created on the
    first ``generate_abstract`` call. With no ``api_key``, the SDK resolves it
    from ``ANTHROPIC_API_KEY`` in the environment (the on-box path). The cloud
    profile, which keeps no secret in the environment, fetches the key from its
    managed secret store and passes it in explicitly.
    """

    def __init__(self, model_id: str, api_key: str | None = None) -> None:
        self._model_id = model_id
        # When None, the SDK resolves ANTHROPIC_API_KEY from the environment;
        # when set (the cloud profile), the value is passed to the SDK directly
        # so it never transits the process environment.
        self._api_key = api_key
        # Deferred: created on first use by _ensure_client(). Holds one
        # AsyncAnthropic instance reused across calls.
        self._client = None
        # Discovered by _resolve_input_limit(). The resolved flag records a
        # settled answer -- a ceiling, a model that reports none, or a lookup
        # failure nothing but a configuration change would clear -- which is
        # what distinguishes "no usable ceiling exists" from "the question is
        # still open". A settled answer is not re-queried per call; an open
        # one is asked again on the next abstract.
        self._input_limit: int | None = None
        self._input_limit_resolved = False
        # Separate from the flag above so the condition is reported once per
        # provider -- which is once per process, the provider being built at
        # startup and shared by every vault -- however many abstracts are
        # generated while it persists. Tied to the resolved flag instead, log
        # economy would hold discovery hostage to it and settle a question
        # that is still open.
        self._input_limit_warned = False
        # Single-flight discovery: concurrent first abstracts spend one lookup
        # between them, and all of them see its result. Scoped to discovery --
        # generation is not serialized, here or behind the local provider's
        # unified-memory lock.
        self._input_limit_lock = asyncio.Lock()

    def _ensure_client(self):
        """Lazily construct and cache the async SDK client.

        The ``anthropic`` import is deferred to call time (mirrors the local
        provider's lazy backend import) so the abstraction construction path
        carries no hard import of the SDK. With no ``api_key`` the SDK resolves
        ``ANTHROPIC_API_KEY`` from the environment; an explicit key is handed to
        the SDK directly and never read from the environment.
        """
        if self._client is None:
            from anthropic import AsyncAnthropic

            if self._api_key is not None:
                self._client = AsyncAnthropic(api_key=self._api_key)
            else:
                self._client = AsyncAnthropic()
        return self._client

    async def _resolve_input_limit(self) -> int | None:
        """Discover the configured model's maximum input size.

        Read from the model registry rather than carried here as a table, so
        the ceiling follows the model roster. Every outcome that leaves no
        usable ceiling -- a lookup that failed, a model that reports none --
        degrades to an unchecked call rather than failing the abstract
        (CAS-ADR-011); the API's own rejection remains the backstop and is
        classified as terminal.

        The lookup is settled once and reused, but only a settled answer
        counts: a successful retrieve, or a failure a later attempt would
        reproduce. A transient failure leaves the question open, so the next
        abstract asks again rather than spending the rest of the process
        submitting unchecked -- which is the whole of the check's value on a
        long-lived server, where the first abstract a replica happens to serve
        would otherwise decide the matter for every abstract after it.

        Returns:
            The model's maximum input size in tokens, or None when no usable
            ceiling could be established.
        """
        if self._input_limit_resolved:
            return self._input_limit

        async with self._input_limit_lock:
            # Re-checked under the lock: a caller that queued here while
            # another was mid-lookup wants that lookup's answer, not one of
            # its own.
            if self._input_limit_resolved:
                return self._input_limit

            client = self._ensure_client()
            try:
                info = await client.models.retrieve(self._model_id)
                limit = info.max_input_tokens
            except Exception as exc:
                self._warn_input_limit_unavailable(
                    "Could not determine the input limit for model %s (%s: %s); "
                    "abstraction input will not be checked before the call",
                    self._model_id,
                    type(exc).__name__,
                    exc,
                )
                self._input_limit_resolved = _discovery_failure_is_permanent(exc)
                return None

            self._input_limit_resolved = True
            if limit is None:
                self._warn_input_limit_unavailable(
                    "Model %s reports no maximum input size; abstraction input "
                    "will not be checked before the call",
                    self._model_id,
                )
                return None

            self._input_limit = limit
            return limit

    def _warn_input_limit_unavailable(self, message: str, *args: object) -> None:
        """Report an absent input ceiling, at most once per provider."""
        if self._input_limit_warned:
            return
        self._input_limit_warned = True
        logger.warning(message, *args)

    async def _count_input_tokens(self, text: str, system_prompt: str) -> int:
        """Exact token count for the request this text would produce."""
        client = self._ensure_client()
        counted = await client.messages.count_tokens(
            model=self._model_id,
            system=system_prompt,
            messages=[{"role": "user", "content": wrap_source_document(text)}],
        )
        return counted.input_tokens

    async def _fit_to_budget(self, text: str, budget: int, system_prompt: str) -> str:
        """Reduce text to fit a token budget, counting only when it may not.

        Returns the text unchanged whenever it already fits, so the ordinary
        document pays nothing for the guard.
        """
        # A token never encodes to fewer than one byte, so text whose UTF-8
        # length is already within the budget cannot overrun it. That holds
        # without a round trip, which keeps counting off the path every
        # ordinary document takes. The measurement is taken on the framed
        # source rather than the bare text because the framing is part of
        # what the request carries: measuring the bare text would let a
        # document sitting just inside the budget skip the count and then
        # exceed it by the width of the markers.
        if len(wrap_source_document(text).encode("utf-8")) <= budget:
            return text

        counted = await self._count_input_tokens(text, system_prompt)
        if counted <= budget:
            return text

        original_tokens = counted
        candidate = text
        for _ in range(_MAX_SHRINK_ROUNDS):
            # Proportional estimate, deliberately undershooting: token density
            # is uneven across a document, so aiming exactly at the budget
            # lands over it about as often as under.
            keep = max(1, int(len(candidate) * (budget / counted) * 0.95))
            candidate = candidate[:keep]
            counted = await self._count_input_tokens(candidate, system_prompt)
            if counted <= budget:
                break
        else:
            # Density uneven enough that the estimates did not converge.
            # Slicing to the budget in bytes satisfies it by construction, on
            # the same one-byte-per-token floor the pre-check rests on.
            candidate = text.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
            counted = await self._count_input_tokens(candidate, system_prompt)

        logger.info(
            "Truncating hosted abstraction input from %d to %d tokens (model=%s, input_budget=%d)",
            original_tokens,
            counted,
            self._model_id,
            budget,
        )
        return candidate

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        """Generate a semantic abstract from document text via a hosted model.

        Input beyond the model's discovered ceiling is reduced to fit and the
        reduction recorded, so an over-large document yields a partial abstract
        rather than failing ingestion. An overrun the reduction did not catch
        is reported as a terminal failure rather than a transient one, because
        it is deterministic in the document and no later attempt resolves it.

        Args:
            text: Full document text from the projection stage.
            max_tokens: Upper bound on abstract length in tokens. Carries the
                density-proportional budget computed upstream; passed straight
                to the Messages API.
            doc_type: The document's type, surfaced to the model so it can
                choose appropriate descriptive verbs. May be None.

        Returns:
            Non-empty abstract string.

        Raises:
            AbstractionInputTooLargeError: If the API rejects the request as
                over-length. Non-retryable.
            RuntimeError: If text is empty or the model produces empty output.
        """
        if not text or not text.strip():
            raise RuntimeError("Cannot generate abstract from empty document text")

        system_prompt = _format_system_prompt(doc_type)

        limit = await self._resolve_input_limit()
        if limit is not None:
            budget = _input_budget_tokens(limit, max_tokens, system_prompt)
            text = await self._fit_to_budget(text, budget, system_prompt)

        client = self._ensure_client()
        try:
            response = await client.messages.create(
                model=self._model_id,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": wrap_source_document(text)}],
            )
        except Exception as exc:
            if _reports_input_too_long(exc):
                raise AbstractionInputTooLargeError(self._model_id, None, None) from exc
            raise

        abstract = next(
            (block.text for block in response.content if block.type == "text"),
            "",
        ).strip()
        if not abstract:
            raise RuntimeError(
                f"Abstraction model returned empty output for {len(text)} chars of input"
            )
        return abstract
