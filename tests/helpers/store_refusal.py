"""Builders for driving the vault-source store's typed refusal.

Shared by every suite that exercises a path where the store can decline: the
source-file repair and audit, the ingest retention, and the delivery reads.
Kept here rather than in one suite's module so the arms stay identical --
a refusal built two slightly different ways is a refusal two suites can
disagree about.
"""

# The store's own body, which names its cause precisely and belongs in the log.
# A sentinel rather than plausible prose so an assertion that it did *not*
# travel onto the public error cannot pass by paraphrase.
STORE_BODY = "quota-exceeded-on-site-contoso-sharepoint-com-drive-b!xyz"


def store_refusal(
    status: int | None,
    *,
    retryable: bool,
    operation: str = "write source",
    target: str = "vault_a/imports/r.md",
):
    """The refusal the real Graph client raises, built the way it builds it.

    ``retryable`` is passed rather than derived from ``status`` on purpose:
    the binding owns the transience decision, and a caller that re-derives it
    from the status at a boundary is the rival the transience tests kill.
    """
    from sage.vault_source_document_store import SourceStoreRefusalError

    return SourceStoreRefusalError(
        f"Microsoft Graph {operation} failed: {status} {STORE_BODY}",
        operation=operation,
        target=target,
        status=status,
        retryable=retryable,
    )
