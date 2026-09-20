"""Hand-maintained register of every divergence between SAGE's request surfaces.

CAS-ADR-052 realizes each caller capability beneath both request surfaces --
the MCP tool surface and the REST (OpenAPI) surface -- and admits a divergence
between them only in a closed set of categories. Where a divergence's category
is arguable, doubt resolves to parity: the divergence is recorded as pending
remediation, never as permanent.

This module is the in-code transcription of the *Surface divergences* register
in the *SAGE MCP Tool Surface* steering document, which is the enumeration's
home. The two move together: a divergence added, recategorized, or remediated
is edited there and here in the same change. The conformance gate in
``test_mcp_tool_conformance.py`` holds the live surfaces to these registers and
refuses any entry that does not name a ``DivergenceCategory``.

What the category requirement buys is legibility, not judgment. The gate cannot
read a basis and decide whether its protocol reason is sound; it can only
insist that one of the admissible reasons is claimed, so a wrong claim is
visible in review rather than persuasive in prose. Pending entries are printed
at the end of every run that exercises the gate, so the backlog of gaps is
visible without opening this file.

Refusal parity -- both surfaces refusing an undeclared argument on the same
terms -- is gated separately, with no exemption list, by
``test_rest_request_strictness_conformance.py``.

Keep the module free of ``sage`` imports: the registers are the test side's
record of a decision, and deriving them from the running system would make the
gate an echo.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Final, NamedTuple


class DivergenceCategory(enum.StrEnum):
    """The categories CAS-ADR-052 admits, plus the one it does not.

    The first four are permanent. ``PENDING_REMEDIATION`` holds everything
    else, including every arguable case, and each such entry names the work
    that closes it.
    """

    TRANSLATION_ARTIFACT = "translation artifact"
    DELIVERY_FORM = "delivery form"
    SINGLE_AUDIENCE_OPERATION = "single-audience operation"
    OPERATION_FACTORING = "operation factoring"
    PENDING_REMEDIATION = "pending remediation"


class Divergence(NamedTuple):
    category: DivergenceCategory
    basis: str
    #: The tool or operation on the *other* surface that carries this
    #: divergence's capability. Required of, and only of, the categories whose
    #: definition asserts the capability is reachable there -- delivery form
    #: and operation factoring. The other three assert no such thing: a
    #: translation artifact compensates for one protocol's clients, a
    #: single-audience operation has one caller class, and a pending
    #: remediation is the admission that nothing carries it yet.
    reached_by: tuple[str, ...] = ()
    #: The component schema carrying the caller-settable options of a delivery
    #: form that encodes them in a transport field the specification types
    #: opaquely -- a JSON envelope in a multipart form field, say, which no
    #: structural reader can follow to its component. Naming it lets the gate
    #: compare those options against ``reached_by``'s arguments, which is the
    #: whole of what "the capability is reachable there" asserts.
    options_schema: str | None = None
    #: Properties of ``options_schema`` that *are* the delivery form rather
    #: than options it carries, and so have no counterpart argument.
    carried_by_form: tuple[str, ...] = ()


_TA = DivergenceCategory.TRANSLATION_ARTIFACT
_DF = DivergenceCategory.DELIVERY_FORM
_SA = DivergenceCategory.SINGLE_AUDIENCE_OPERATION
_OF = DivergenceCategory.OPERATION_FACTORING
_PR = DivergenceCategory.PENDING_REMEDIATION


# ---------------------------------------------------------------------------
# MCP-only arguments: (surface, tool, argument) -> Divergence
# ---------------------------------------------------------------------------

_DOC_ID_ALIAS = Divergence(
    _TA,
    "Inbound alias for document_id. Published so a client that coerces arguments "
    "to the published schema does not strip the shorthand before dispatch.",
)

_METADATA_TRIPWIRE = Divergence(
    _TA,
    "Tripwire for a metadata key spelled at the top level (CAS-ADR-037). Published "
    "only so a client's schema coercion does not strip the mistake before the "
    "misplaced_metadata guard can refuse it; a REST caller reads the OpenAPI schema "
    "and is refused by the undeclared-field check instead.",
)

_FILTER_TRIPWIRE = Divergence(
    _TA,
    "Tripwire for a filters key spelled at the top level (CAS-ADR-037). Published "
    "only so a client's schema coercion does not strip the mistake before the "
    "misplaced_filters guard can refuse it; a REST caller reads the OpenAPI schema "
    "and is refused by the undeclared-field check instead.",
)

_SPILL_DELIVERY = Divergence(
    _DF,
    "Spill-to-file delivery exists because of MCP tool-result size ceilings. A REST "
    "caller receives the whole projection in the response body, so there is nothing "
    "to spill.",
)

MCP_ONLY_ARGUMENTS: Final[dict[tuple[str, str, str], Divergence]] = {
    ("sage_core", "traverse", "document_id"): Divergence(
        _TA,
        "Inbound alias for start_id, unifying document-id parameter naming across "
        "the tools for callers that guess field names.",
    ),
    ("sage_core", "get_document", "doc_id"): _DOC_ID_ALIAS,
    ("sage_core", "read_section", "doc_id"): _DOC_ID_ALIAS,
    ("sage_core", "list_headings", "doc_id"): _DOC_ID_ALIAS,
    ("sage_core", "chain", "doc_id"): _DOC_ID_ALIAS,
    ("sage_core", "read_projection", "doc_id"): _DOC_ID_ALIAS,
    ("sage_core", "read_projection", "write_to_path"): _SPILL_DELIVERY,
    ("sage_core", "read_projection", "delivery"): _SPILL_DELIVERY,
    **{
        ("sage_core", "ingest_document", key): _METADATA_TRIPWIRE
        for key in (
            "title",
            "version_label",
            "project",
            "doc_type",
            "authority_scope",
            "document_date",
            "tags",
        )
    },
    **{
        ("sage_core", "search", key): _FILTER_TRIPWIRE
        for key in (
            "doc_type",
            "project",
            "lifecycle_status",
            "exclude_terminal_lifecycle",
            "tags",
            "document_ids",
            "pipeline_status",
            "source_type",
            "tier3_metadata",
            "source_id",
            "target_id",
            "edge_type",
        )
    },
}


# ---------------------------------------------------------------------------
# REST-only arguments: (surface, tool, argument) -> Divergence
# ---------------------------------------------------------------------------

# Empty: every argument of every REST operation with an MCP counterpart
# appears on that tool.
REST_ONLY_ARGUMENTS: Final[dict[tuple[str, str, str], Divergence]] = {}


# ---------------------------------------------------------------------------
# MCP-only tools: (surface, tool) -> Divergence
# ---------------------------------------------------------------------------

MCP_ONLY_TOOLS: Final[dict[tuple[str, str], Divergence]] = {
    ("sage_core", "update_staging_edge"): Divergence(
        _OF,
        "One tool selecting by its action argument; REST offers the same capability "
        "as the discrete confirm_staging_edge and dismiss_staging_edge operations.",
        reached_by=("confirm_staging_edge", "dismiss_staging_edge"),
    ),
}


# ---------------------------------------------------------------------------
# REST-only operations: (surface, operation_id) -> Divergence
# ---------------------------------------------------------------------------

_STAGING_FACTORING = Divergence(
    _OF,
    "Discrete REST operation; MCP reaches the same capability through "
    "update_staging_edge(action=...).",
    reached_by=("update_staging_edge",),
)

_EDITOR_MODEL = Divergence(
    _PR,
    "Editor-model write control, forward-declared and unbuilt on either surface. "
    "Closes when it is built beneath both surfaces.",
)

_ROOT_HARNESS = Divergence(
    _PR,
    "The ROOT Harness Orchestration API has no MCP surface yet. Closes when the "
    "orchestrator tools are built.",
)

REST_ONLY_OPERATIONS: Final[dict[tuple[str, str], Divergence]] = {
    ("sage_core", "confirm_staging_edge"): _STAGING_FACTORING,
    ("sage_core", "dismiss_staging_edge"): _STAGING_FACTORING,
    ("sage_core", "register_user"): Divergence(
        _SA,
        "CAS Application account creation. Agents pass created_by strings per "
        "CAS-ADR-021 and have no account to register.",
    ),
    ("sage_core", "open_document"): Divergence(_SA, "Browser UI affordance."),
    ("sage_core", "get_document_download_url"): Divergence(
        _DF,
        "Mints a short-lived URL a browser fetches directly from the backing store. "
        "MCP reaches source bytes through get_document and its download recipe.",
        reached_by=("get_document",),
    ),
    ("sage_core", "get_document_content"): Divergence(
        _DF,
        "Streams retained source bytes as a raw download. A raw byte stream has no MCP "
        "tool-result form; MCP reaches the bytes through get_document and its download "
        "recipe.",
        reached_by=("get_document",),
    ),
    ("sage_core", "transfer_upload"): Divergence(
        _DF,
        "Byte leg of the caller-local transfer channel: the raw PUT body delivered "
        "against a one-time upload token. The recipe and completion legs are on both "
        "surfaces; a raw byte stream has no MCP tool-result form.",
        reached_by=("ingest_document", "bulk_ingest_document"),
    ),
    ("sage_core", "transfer_download"): Divergence(
        _DF,
        "Byte leg of the caller-local transfer channel: streams a pending transfer's "
        "bytes against a one-time download token. The recipe leg is on both surfaces; "
        "a raw byte stream has no MCP tool-result form.",
        reached_by=("get_document",),
    ),
    ("sage_core", "batch_ingest_documents"): Divergence(
        _DF,
        "Multipart upload with an event-stream response (CAS-ADR-042). MCP reaches "
        "caller-delivered bulk ingest through bulk_ingest_document and the transfer "
        "channel.",
        reached_by=("bulk_ingest_document",),
        options_schema="BatchIngestUploadMetadata",
    ),
    ("sage_core", "get_editors"): _EDITOR_MODEL,
    ("sage_core", "set_editors"): _EDITOR_MODEL,
    ("cas_app", "begin_login"): Divergence(
        _SA, "Browser-interactive sign-in entry: an identity-provider redirect and a cookie."
    ),
    ("cas_app", "get_session"): Divergence(_SA, "Cookie-scoped session-state read for the SPA."),
    ("cas_app", "end_session"): Divergence(_SA, "Cookie-scoped sign-out."),
    **{
        ("root_harness", operation_id): _ROOT_HARNESS
        for operation_id in (
            "trigger_workflow",
            "get_status",
            "approve",
            "list_pending",
            "subscribe_events",
            "register_agent",
            "get_agent",
            "get_agent_history",
            "get_pipeline_status",
        )
    },
}


#: Every register, named as its heading reads in the steering document.
REGISTERS: Final[tuple[tuple[str, Mapping[tuple[str, ...], object]], ...]] = (
    ("MCP_ONLY_TOOLS", MCP_ONLY_TOOLS),
    ("REST_ONLY_OPERATIONS", REST_ONLY_OPERATIONS),
    ("MCP_ONLY_ARGUMENTS", MCP_ONLY_ARGUMENTS),
    ("REST_ONLY_ARGUMENTS", REST_ONLY_ARGUMENTS),
)


def uncategorized(
    registers: tuple[tuple[str, Mapping[tuple[str, ...], object]], ...],
) -> list[str]:
    """Entries that do not name an admissible category with a basis.

    Membership is tested by type, not equality: a ``StrEnum`` member compares
    equal to its raw string, so an entry written ``"delivery form"`` would
    pass an equality test while bypassing the enumeration entirely.
    """
    offenders: list[str] = []
    for register_name, register in registers:
        for key, entry in register.items():
            if not isinstance(entry, Divergence):
                offenders.append(f"{register_name}{key!r}: not a Divergence")
            elif not isinstance(entry.category, DivergenceCategory):
                offenders.append(
                    f"{register_name}{key!r}: category {entry.category!r} is not a "
                    "DivergenceCategory member"
                )
            elif not entry.basis.strip():
                offenders.append(f"{register_name}{key!r}: empty basis")
    return offenders


def pending_remediation_lines(
    registers: tuple[tuple[str, Mapping[tuple[str, ...], object]], ...],
) -> list[str]:
    """One line per entry recorded as pending remediation, naming its basis."""
    return [
        f"{register_name} {'/'.join(key)}: {entry.basis}"
        for register_name, register in registers
        for key, entry in register.items()
        if isinstance(entry, Divergence)
        and entry.category is DivergenceCategory.PENDING_REMEDIATION
    ]
