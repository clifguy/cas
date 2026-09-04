"""Translation of vault-source store refusals into typed API errors (CAS-ADR-043).

The vault-source store binding refuses an operation with its own exception:
it sits below the API layer and may not import the error hierarchy. Left to
propagate, that refusal reaches an MCP caller as a generic internal error and
an HTTP caller as a bare 500 against a spec declaring neither -- describing a
server fault rather than the upstream refusal it actually is.

More than one service writes through that binding -- an ingest retains a
source, a restore writes one back -- and both owe a caller the same answer for
the same upstream fact. The translation lives here rather than in either of
them so the two cannot drift apart on which refusals are worth retrying or on
how much of the store's own text reaches a caller.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

from sage.api.errors import VaultSourceStoreRefusedError, VaultSourceStoreUnavailableError

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def translate_store_refusal(source_path: str) -> Iterator[None]:
    """Surface the vault-source store's refusal as a typed API error.

    The binding has already decided whether waiting could change the answer,
    which is the one thing a caller needs and the one thing a message cannot
    convey reliably; that decision picks between the two codes.

    The store's own response body is logged rather than forwarded. It names
    the cause far better than anything composed here could, which is why it is
    kept at all -- but it is the store's text rather than this API's, it can
    carry tenant coordinates, and forwarding it would make an upstream
    service's wording a declared part of this surface.

    ``source_path`` is whichever path the caller can act on: the document
    record's own vault-relative path where a repair names its target, the
    caller's own spelling of the source where a retain does. Never a
    server-side location, which is the property both callers share and the one
    that matters -- a staged upload's path under this process's temp tree names
    nothing the caller has.
    """
    from sage.vault_source_document_store import SourceStoreRefusalError

    try:
        yield
    except SourceStoreRefusalError as exc:
        logger.warning("vault-source store refused %s for %s: %s", exc.operation, source_path, exc)
        error = VaultSourceStoreUnavailableError if exc.retryable else VaultSourceStoreRefusedError
        raise error(source_path, exc.operation, exc.status) from exc
