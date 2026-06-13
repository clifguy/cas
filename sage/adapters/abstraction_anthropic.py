"""Hosted AbstractionProvider implementation via the Anthropic Messages API.

Generates a density-proportional semantic abstract with a hosted Claude
model (e.g. Haiku) behind the same ``AbstractionProvider`` contract as the
local Qwen3 provider. Because the call is a network request with no
unified-memory budget, it is deliberately not serialized behind the local
provider's ``_generation_lock`` (the F-8 guard). Making abstraction
selectable to a hosted call is what lets the SAGE core run on a non-Apple
target (CAS-ADR-042).
"""

import logging

from sage.adapters.abstraction_prompt import _format_system_prompt
from sage.adapters.interfaces import AbstractionProvider

logger = logging.getLogger(__name__)


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

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        """Generate a semantic abstract from document text via a hosted model.

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
            RuntimeError: If text is empty or the model produces empty output.
        """
        if not text or not text.strip():
            raise RuntimeError("Cannot generate abstract from empty document text")

        client = self._ensure_client()
        response = await client.messages.create(
            model=self._model_id,
            max_tokens=max_tokens,
            system=_format_system_prompt(doc_type),
            messages=[{"role": "user", "content": text}],
        )

        abstract = next(
            (block.text for block in response.content if block.type == "text"),
            "",
        ).strip()
        if not abstract:
            raise RuntimeError(
                f"Abstraction model returned empty output for {len(text)} chars of input"
            )
        return abstract
