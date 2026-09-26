"""SAGE protocol and API tools for MCP.

Contains the protocol tools (ingest, get, update, lifecycle, link,
traverse, discover, export, refresh) and API query tools (vault stats,
hash check, staging edges, pending metadata) over the SAGE services.

A tool translates its MCP arguments into a service call and the result
into the MCP envelope. Boundary shape validation belongs here, as does a
divergence from the REST surface in a category CAS-ADR-052 admits. New
semantic validation, defaulting, refusal, or other behavior belongs in
``sage.services``, beneath both this surface and the REST routers, so
the two answer alike.
"""

import logging
from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from sage._mcp_item_schema import published_item_list
from sage._mcp_param import (
    DOC_ID_ALIAS_NOTE,
    DocIdAliasParam,
    NumericText,
    VaultIdParam,
    model_param,
    param_doc,
    shaped_param,
)
from sage._tool_annotations import READ_ONLY, WRITE_ADDITIVE, WRITE_DESTRUCTIVE
from sage.api.errors import (
    AmbiguousDocumentIdentifierError,
    InvalidActionError,
    LegacyFormError,
    MisplacedFilterError,
    MisplacedMetadataError,
    MissingDocumentIdentifierError,
    SAGEError,
    UndeclaredKeyError,
    translate_validation_error,
    validation_error_envelope,
)
from sage.mcp_init import SAGEServices
from sage.models.enums import (
    CatalogSortBy,
    EdgeType,
    FacetField,
    ResponseMode,
    RetrievalMode,
    SortOrder,
    SourceType,
    TraversalDirection,
)
from sage.models.legacy_form import detect_legacy_form
from sage.models.schemas import (
    BATCH_ITEM_MODELS,
    BulkLifecycleItem,
    BulkLifecycleRequest,
    BulkLinkItem,
    BulkLinkRequest,
    BulkMetadataItem,
    BulkMetadataRequest,
    ChainRequest,
    CreateVaultRequest,
    DiscoverRequest,
    DocumentDateStr,
    DocumentIdStr,
    EdgeIdStr,
    ExportProjectionRequest,
    HashCheckRequest,
    IngestPreview,
    IngestRequest,
    OptimizeContentStoreRequest,
    ParseFilenameRequest,
    ReabstractRequest,
    RecomputePipelineStartedResponse,
    RetrievalFilters,
    Sha256Str,
    SourceFileIntegrityRequest,
    SourceFileRestoreRequest,
    StagingEdgeConfirmRequest,
    TraverseRequest,
    UpdateVaultConfigRequest,
    UploadRecipe,
    VaultIdStr,
)
from sage.services.metadata import PENDING_METADATA_DEFAULT_LIMIT, PENDING_METADATA_MAX_LIMIT
from sage.services.retrieval import DEFAULT_MCP_INLINE_BUDGET_BYTES
from sage.services.stack_config import get_stack_config_report
from sage.services.transfer import DeliveryDeclaration
from sage.services.vault_registry import VaultRegistryService

logger = logging.getLogger(__name__)

# Module-scope TypeAdapters for Pattern 2 boundary validation on FastMCP tool
# parameters. Each adapter is constructed once at import time and reused per
# tool call. ``validate_python`` raises ``pydantic_core.ValidationError`` (a
# ``ValueError`` subclass) on shape failures; every tool's
# ``except (SAGEError, ValueError)`` block catches it and routes through
# ``error_response(...)`` so MCP callers see a uniform error envelope.
_VAULT_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(VaultIdStr)
_DOCUMENT_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(DocumentIdStr)
_EDGE_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(EdgeIdStr)
_DOCUMENT_DATE_ADAPTER: TypeAdapter[str | None] = TypeAdapter(DocumentDateStr)
_SHA256_ADAPTER: TypeAdapter[str] = TypeAdapter(Sha256Str)

# Each batch argument publishes the shape its tool body validates an item
# against, while still arriving as plain mappings (see ``sage._mcp_item_schema``).
_BATCH_SHAPE_NOTE = (
    "Shape validation runs up front: one malformed item refuses the whole "
    "batch before any per-item work."
)
_LIFECYCLE_ITEMS = published_item_list(
    BulkLifecycleItem,
    description=param_doc(
        BulkLifecycleRequest,
        "items",
        mcp=(
            f"{_BATCH_SHAPE_NOTE} Each item carries ``document_id``, ``action``, "
            "and optional ``successor_id`` or ``relocated_to``. A state or "
            "transition listing ``doc_types`` applies only to those doc_types. "
            "``supersede`` is admitted from whichever ``from_state`` rows the "
            "vault's table declares for it and lands the predecessor in that "
            "row's ``to_state``, so a vault declaring ``completed --supersede--> "
            "archived`` supersedes a completed predecessor directly; an "
            "already-superseded predecessor refuses with "
            "``invalid_lifecycle_transition``. To repair an "
            "archived predecessor missing its supersedes edge, add the edge with "
            "``create_edges``, which transitions no lifecycle. ``relocate`` moves "
            "the chain head to the terminal ``relocated`` state, which no action "
            "leaves; perform the destination half first, as an "
            "``ingest_document`` carrying ``relocated_from``. A ``relocated_to`` "
            "whose ``source_content_hash`` matches neither the document's "
            "provenance digest nor its as-stored digest refuses with "
            "``relocated_to_provenance_mismatch``, its detail naming them as "
            "``document_content_hash`` and ``also_accounted_content_hash``. "
            "A per-item error surfaces in the response's per-item error "
            "envelope rather than as a batch-level 400/409."
        ),
    ),
)
_LINK_ITEMS = published_item_list(
    BulkLinkItem,
    description=param_doc(
        BulkLinkRequest,
        "items",
        mcp=(
            f"{_BATCH_SHAPE_NOTE} A per-item error surfaces in the response's "
            "per-item error envelope rather than as a batch-level 400. "
            "Per-item anchor fields by edge_type policy "
            "bucket: ``none`` (supersedes, retracts, merged_from) takes no "
            "anchor fields, except that ``retracts`` takes a one-sided "
            "``source_valid_from_version`` and ``retracted_edge_id`` (no "
            "``target_id``); ``transitive_source`` (derived_from) requires "
            "``source_valid_from_version`` only; ``transitive_both`` (covers, "
            "references, bundles_with, depends_on, instantiated_from) requires "
            "both ``source_valid_from_version`` and "
            "``target_valid_from_version``. An anchor field takes a document "
            "id, not a version label: each anchor must name a document in its "
            "endpoint's ``supersedes`` lineage. To link whole documents, pass "
            "the endpoint's own id as its anchor. For example: "
            'edge_type="derived_from", source_id="<deliverable_id>", '
            'target_id="<template_id>", '
            'source_valid_from_version="<deliverable_id>"; and '
            'edge_type="references", source_id="<source_id>", '
            'target_id="<target_id>", source_valid_from_version="<source_id>", '
            'target_valid_from_version="<target_id>". ``merged_from`` '
            "chain-head precondition: neither endpoint may have an outbound "
            "``supersedes`` edge, or the item returns merged_from_validation; "
            "for content reuse from a mid-chain source, use ``derived_from``, "
            "whose anchor carries the chain visibility ``merged_from`` lacks."
        ),
    ),
)
_METADATA_ITEMS = published_item_list(
    BulkMetadataItem,
    description=param_doc(
        BulkMetadataRequest,
        "items",
        mcp=(
            f"{_BATCH_SHAPE_NOTE} ``tags`` takes a ``ListFieldPatch`` "
            'ops-object, {"add": ["x"], "remove": ["y"]} -- at least one key, '
            "``add`` values absent and ``remove`` values present; "
            "``tier3_metadata`` takes a ``Tier3Patch``, "
            '{"set": {"key": "value"}, "unset": ["other_key"]}, the merged '
            "result validated against the resolved doc_type's "
            "``metadata_schema``. The bare-list / bare-dict forms are rejected "
            "with ``legacy_form``. ``lifecycle_state_not_applicable`` refuses a "
            "``doc_type`` change while the document holds a lifecycle state the "
            "new doc_type cannot hold; transition it first."
        ),
    ),
)

#: What a batch tool's ``response_mode`` adds on the MCP surface.
_BATCH_RESPONSE_MODE_NOTE = (
    f"The inline budget is {DEFAULT_MCP_INLINE_BUDGET_BYTES:,} bytes, set per "
    "process by SAGE_MCP_INLINE_BUDGET_BYTES. An invalid value is refused as "
    "invalid_parameter before any per-item work."
)

#: How a download-capable ``write_to_path`` behaves on each delivery arm,
#: shared by the tools that can mint a download recipe. The sentence before
#: it, naming what the local arm writes, is each tool's own.
_WRITE_TO_PATH_RECIPE = (
    "Where the server shares the caller's filesystem, the target must not "
    "exist (write_path_exists) and its parent must exist and be "
    "writable, and a failure to open the target for exclusive creation "
    "reports write_path_invalid. Where the server does not share the caller's "
    "filesystem, the response is a download recipe carrying this path for the "
    "caller's own environment to write, and the path is read with that "
    "environment's conventions -- a Windows drive-letter or UNC spelling is "
    "accepted on that arm. The path must be absolute either way, and is "
    "checked before the {subject} is read, so a malformed path reports "
    "write_path_invalid {regardless}. A minted recipe's token lapses 900 "
    "seconds after issue by default and the fetch must finish inside that "
    "window; the recipe's own `expires_at` is authoritative where a "
    "deployment has tuned the lifetime, and a lapsed recipe is re-issued "
    "rather than resumed."
)

_GET_DOCUMENT_WRITE_TO_PATH = (
    "Absolute filesystem path, resolved on the machine running the SAGE server "
    "process: SAGE streams the retained source bytes there (from the vault's "
    "configured source store, unbounded by the inline-content ceiling) and "
    "populates `written_to`, `content_size` and `content_hash`. "
    + _WRITE_TO_PATH_RECIPE.format(
        subject="document", regardless="whether or not the document exists"
    )
    + " Mutually exclusive with `include_content`."
)

_READ_PROJECTION_WRITE_TO_PATH = (
    "Absolute filesystem path, resolved on the machine running the SAGE server "
    "process: SAGE writes the projection text there and returns metadata only "
    "(`written_to`, `content_size`; `projection_text` is null). "
    + _WRITE_TO_PATH_RECIPE.format(
        subject="projection", regardless="whatever the document's pipeline state"
    )
)

#: ``traverse.direction`` is published as a bare string, so its closed set is
#: named in the description, from the enum ``TraverseRequest`` validates.
_DIRECTION_NOTE = "One of " + ", ".join(f'"{d.value}"' for d in TraversalDirection) + "."

#: ``traverse.depth`` is published as a bare integer; its range is read from
#: the bounds ``TraverseRequest`` validates.
_DEPTH_BOUNDS = {type(m).__name__: m for m in TraverseRequest.model_fields["depth"].metadata}
_DEPTH_NOTE = f"Range {_DEPTH_BOUNDS['Ge'].ge}-{_DEPTH_BOUNDS['Le'].le}."

#: A bare-string ``response_mode`` names its closed set from ``ResponseMode``.
_RESPONSE_MODE_NOTE = "One of " + " or ".join(f'"{m.value}"' for m in ResponseMode) + "."

#: The edge-walking tools take ``edge_type`` as a bare string; its closed set is
#: named from the enum their request models validate.
_EDGE_TYPE_NOTE = "One of " + ", ".join(f'"{e.value}"' for e in EdgeType) + "."

#: The verbs ``update_staging_edge`` accepts; any other is refused.
STAGING_EDGE_ACTIONS: tuple[str, ...] = ("confirm", "dismiss")
# Collection parameters carry the alias on the element type, so the adapter
# wraps the sequence rather than the alias. Validation is whole-argument: one
# unusable entry fails the call rather than being dropped from the batch.
_SHA256_LIST_ADAPTER: TypeAdapter[list[str]] = TypeAdapter(list[Sha256Str])
_DOCUMENT_ID_LIST_ADAPTER: TypeAdapter[list[str]] = TypeAdapter(list[DocumentIdStr])


def _check_legacy_patch_form(field: str, value: object) -> None:
    """Raise ``LegacyFormError`` when ``value`` is the pre-patch shape for ``field``.

    Thin wrapper around ``detect_legacy_form`` that translates the
    pure-data detection result into the public-facing exception. The
    MCP tool surface uses this directly; the FastAPI surface goes
    through ``PydanticCustomError(type='legacy_form')`` raised inside
    request-model validators, which ``translate_validation_error``
    converts to ``LegacyFormError`` for the wire.
    """
    details = detect_legacy_form(field, value)
    if details is not None:
        raise LegacyFormError(
            field=details.field,
            received_type=details.received_type,
            example=details.example,
        )


# The closed set of caller-supplied metadata keys ``ingest_document``
# recognizes inside its ``metadata`` argument, in canonical order. Each is
# also published as a top-level tool parameter so that a caller who spells
# it at the wrong level reaches ``_check_misplaced_metadata`` below rather
# than having the value stripped in transit: MCP clients coerce arguments
# to the published schema and drop unknown properties, which would discard
# the field before the server could object (CAS-ADR-037 covers the
# framework-level rejection that this publication step makes reachable).
_INGEST_METADATA_KEYS: tuple[str, ...] = (
    "title",
    "version_label",
    "project",
    "doc_type",
    "authority_scope",
    "document_date",
    "tags",
)

_MISPLACED_METADATA_EXAMPLE = 'metadata={"title": "...", "tags": ["..."]}'

# The filter keys ``search`` recognizes inside its ``filters`` argument,
# derived from the model that defines them so the two cannot drift. Each is
# also published as a top-level tool parameter for the same reason the
# ingest metadata keys are: a wrong-level spelling must reach
# ``_check_misplaced_filters`` below rather than being stripped in transit.
# The whole vocabulary is protected rather than a chosen subset, which
# leaves no key whose flat spelling still returns an unfiltered result set.
# What remains outside the guard is a *misspelled* key, which no
# publication strategy can cover -- but that case loses a constraint the
# caller never successfully named, while a correctly spelled key at the
# wrong level looked like it had been honored.
_SEARCH_FILTER_KEYS: tuple[str, ...] = tuple(RetrievalFilters.model_fields)

_MISPLACED_FILTERS_EXAMPLE = 'filters={"doc_type": "adr", "lifecycle_status": "active"}'

# The annotation every tripwire parameter carries. Two things are load-bearing
# and neither may be narrowed independently of the other.
#
# The type is deliberately absent. A tripwire is never consumed -- any
# non-null value is refused outright -- so validating its shape would replace
# the actionable misplaced-field message with a format complaint about a value
# that was well-formed and merely in the wrong place. Untyped, the published
# schema is the description and a null default alone: a client forwards every
# JSON value against it unchanged, and a client that presents the raw schema
# to its model pays for no union.
#
# The description is what a caller reading the published schema sees. Without
# it the tripwires are indistinguishable from functional arguments, and the
# publication that makes a wrong-level spelling rejectable also invites the
# spelling in the first place. It changes no coercion or rejection behaviour.
#
# It points at the structured error code rather than at CAS-ADR-037, which
# governs the rejection this marking describes. The description ships in the
# tool catalog, where a reader cannot resolve an ADR id; the rationale anchor
# belongs on this surface instead, which is durable and not published.
_INGEST_TRIPWIRE = Annotated[
    Any,
    Field(
        description=(
            "Tripwire, not a functional argument; use metadata={...} (misplaced_metadata)."
        )
    ),
]

_SEARCH_TRIPWIRE = Annotated[
    Any,
    Field(
        description=("Tripwire, not a functional argument; use filters={...} (misplaced_filters).")
    ),
]


def _discover_param(annotation: object, field: str, *, mcp: str | None = None) -> object:
    """``annotation`` published with the ``search`` request field's description."""
    return model_param(annotation, DiscoverRequest, field, mcp=mcp)


def _ingest_param(annotation: object, field: str, *, mcp: str | None = None) -> object:
    """``annotation`` published with the ``ingest_document`` request field's description."""
    return model_param(annotation, IngestRequest, field, mcp=mcp)


# Published on the parameter so a caller reading the tool schema alone
# sees what an omitted mode resolves to. The signature has to admit None
# for the resolution to be expressible at all, and a bare nullable enum
# defaulting to null says nothing about which value fills it in.
_SEARCH_MODE = _discover_param(
    RetrievalMode | None,
    "mode",
    mcp=(
        "Supports four modes: semantic is vector similarity search, optionally "
        "fused with BM25 keyword matching, and returns ranked chunks with "
        "relevance scores; keyword is BM25 only; catalog enumerates by filter; "
        "deterministic is exact content extraction by document ID and heading "
        "path, used for governed content transfer between documents or agents."
    ),
)

_SEARCH_QUERY = _discover_param(
    Annotated[str | None, NumericText],
    "query",
    mcp=(
        "In keyword mode, terms are conjunctive: a document matches only if it "
        "carries every term, so each term added narrows the result and can "
        "empty it. The terms need not appear together in one passage, but the "
        "document is ranked by its best-matching passage, which is the excerpt "
        "returned. A quoted phrase must match inside one passage. "
        "Terms joined by or admit either, each satisfied across the document "
        "as a bare term is, so adding an alternative widens. A term prefixed "
        'with "-" is excluded, and a query carrying an exclusion is satisfied '
        "within a single passage, terms and alternatives alike, so appending "
        "an excluded term can drop a document the same query without it "
        "matched. When a query of bare terms returns nothing, hints.warnings "
        "names the terms the query parsed to: stopwords are dropped and the "
        'rest stemmed. Use query="*" in keyword mode for a filter-only '
        "listing."
    ),
)

_SEARCH_FILTERS = _discover_param(
    dict | None,
    "filters",
    mcp=(
        "Document-target keys: ``doc_type``, ``project``, ``lifecycle_status``, "
        "``exclude_terminal_lifecycle``, ``tags``, ``document_ids``, "
        "``pipeline_status``, ``source_type``, ``tier3_metadata``, ``provenance`` (these also "
        'narrow the faceted slice). Edge-target keys (target="edges"): '
        "``source_id``, ``target_id``, ``edge_type``; mixing the two sets is "
        "refused. Every key belongs nested here, e.g. "
        'filters={"doc_type": "adr", "lifecycle_status": "active"}; a key '
        "passed at the top level is refused with misplaced_filters rather than "
        "dropped. ``source_type`` takes one of markdown, docx, pdf, email, "
        "onenote, teams_chat, xlsx, pptx, structured_data, and ``edge_type`` is "
        "closed the same "
        "way; anything else is refused with invalid_filter_value. "
        "``exclude_terminal_lifecycle`` drops documents in a state the vault "
        "declares terminal. ``tier3_metadata`` takes field-to-value pairs that "
        "must all match exactly; null matches a null or absent field. "
        "``provenance`` matches created_/last_modified_ by, client or agent "
        "likewise; agents by name."
    ),
)

_SEARCH_LIMIT = _discover_param(
    int,
    "limit",
    mcp=(
        "Range 0-100; default 10. Catalog mode supports pagination via limit + "
        "offset. Pass limit=0 to receive total_available alone, with no "
        "results, as an existence or count check."
    ),
)

_SEARCH_SCOPE = _discover_param(
    str,
    "scope",
    mcp=(
        "Results can be scoped (all, authoritative, specific, filtered) and "
        "filtered by metadata criteria."
    ),
)

_SEARCH_SORT_BY = _discover_param(
    str | None,
    "sort_by",
    mcp=f"One of: {', '.join(m.value for m in CatalogSortBy)}.",
)

_SEARCH_SORT_ORDER = _discover_param(
    str | None,
    "sort_order",
    mcp=f"One of: {', '.join(m.value for m in SortOrder)}.",
)

_SEARCH_FACET_VALUE_LIMIT = _discover_param(
    int | None,
    "facet_value_limit",
    mcp=(
        "The facets value cap is denominated in values rather than bytes, so a "
        "facets response whose serialized size exceeds the MCP inline ceiling "
        "carries a facets_response_exceeds_inline_budget hint, naming the "
        "recommended_facet_value_limit to re-call at whenever a smaller cap "
        "would fit and omitting it when none would."
    ),
)

_SEARCH_RESPONSE_MODE = _discover_param(
    str | None,
    "response_mode",
    mcp=(
        "Left unset, a documents-target response over the MCP inline budget is "
        "fitted and says so in hints: catalog returns the light shape "
        "(catalog_response_degraded_to_light); semantic and keyword cut each "
        "long chunk_content to one excerpt length, never fewer than 200 "
        "characters (scored_response_excerpted) -- read a cut passage whole "
        'with mode="deterministic". Where neither fits, '
        "hints.recommended_limit names a size that does. An explicit "
        "response_mode suppresses both. The budget is 45,000 bytes, set per "
        "process by SAGE_MCP_INLINE_BUDGET_BYTES."
    ),
)


#: How long an upload recipe's token lives, shared by the tools that mint one.
_UPLOAD_RECIPE_LIFETIME = (
    "An upload recipe's tokens lapse 900 seconds after issue by default "
    "(its expires_at is authoritative), and the byte delivery and the "
    "completion call must both finish inside that window. A token is also "
    "reclaimed once 3 refused deliveries have been made against it by "
    "default. A lapsed recipe cannot be resumed: re-issue this call."
)

_INGEST_SOURCE = _ingest_param(
    str | None,
    "source",
    mcp=(
        "Sharing a machine means the caller's machine is the machine running "
        "the SAGE server process. The retained copy is authoritative after "
        "ingest, and the path passed here is temporary. " + _UPLOAD_RECIPE_LIFETIME
    ),
)

#: The source formats an adapter is registered for, each with the extensions it
#: is inferred from, as the parameter descriptions name them. Held equal to the
#: adapter registry by test rather than read from it, so loading the tool module
#: instantiates no adapter.
_REGISTERED_SOURCE_FORMATS = (
    "markdown (.md, .markdown), docx (.docx, .dotx), xlsx (.xlsx), "
    "pptx (.pptx, .potx), pdf (.pdf) and "
    "structured_data (.json, .jsonl, .yaml, .yml, .toml, .xml)"
)

_INGEST_SOURCE_TYPE = _ingest_param(
    str | None,
    "source_type",
    mcp=(
        f"The registered formats are {_REGISTERED_SOURCE_FORMATS}. An omitted "
        "type is inferred from the extensions listed with each format; a "
        "refusal names the formats in ``registered_source_types`` and their "
        "extensions in ``registered_extensions``."
    ),
)

_INGEST_CONFIG = _ingest_param(
    dict | None,
    "config",
    mcp=(
        "Deep-merged over the vault's adapter defaults, whose per-adapter shape "
        "is ``adapter_defaults`` in ``get_vault_config``; a key the adapter does "
        "not read is ignored."
    ),
)

_INGEST_PREDECESSOR_ID = _ingest_param(
    str | None,
    "predecessor_id",
    mcp=(
        "An omitted ``doc_type``, ``project`` or ``authority_scope`` inherits "
        "the predecessor's value. A ``supersede_target_not_active`` refusal "
        "carries the ``allowed_states``; move the predecessor to one with "
        "``update_lifecycles`` (``reactivate`` from ``completed``) rather than "
        "archiving it first."
    ),
)

_INGEST_EXPECTED_HEAD_VERSION = _ingest_param(
    str | None,
    "expected_head_version",
    mcp="Pass the ``updated_at`` observed on a prior ``get_document`` read.",
)

_INGEST_NEEDS_REVIEW = _ingest_param(
    bool,
    "needs_review",
    mcp=(
        "The fields filename inference fills are vault configuration "
        "(``metadata_extraction.filename_extraction`` in ``get_vault_config``); "
        "``get_filename_metadata`` shows its suggestions without queuing."
    ),
)

_INGEST_METADATA = _ingest_param(
    dict | None,
    "metadata",
    mcp=(
        "Recognized keys: ``title``, ``version_label``, ``project``, "
        "``doc_type``, ``authority_scope``, ``document_date``, ``tags`` -- e.g. "
        'metadata={"title": "...", "tags": ["..."]}. A key passed at the top '
        "level is refused with misplaced_metadata naming every misplaced key; "
        "an argument this tool declares -- notably ``tier3_metadata`` -- "
        "spelled inside ``metadata`` is refused with misplaced_top_level_field."
    ),
)

_INGEST_TIER3_METADATA = _ingest_param(
    dict | None,
    "tier3_metadata",
    mcp=(
        "Tier3 uniqueness: a doc_type declaring a unique constraint on a tier3 "
        "field enforces per-vault uniqueness on it at ingest time, checked in "
        "the same transaction as the row insert so the existing document is "
        "never disturbed; force does not override it. Call with dry_run=true "
        "to read which fields a doc_type declares unique, under "
        "requirements.unique_tier3_fields, and which it requires. Queryable "
        'via ``search`` as filters={"tier3_metadata": {...}}.'
    ),
)

_INGEST_RELOCATED_FROM = _ingest_param(
    dict | None,
    "relocated_from",
    mcp=(
        'Shape: {"vault_id", "document_id", "server_address", '
        '"source_content_hash", "relocated_at"}; server_address may be null. '
        "The content hash must match the digest this vault records for the "
        "source, or the call is refused with relocated_from_provenance_mismatch. "
        'The origin half is ``update_lifecycles`` with action="relocate".'
    ),
)

_INGEST_TRANSFER_TOKEN = _ingest_param(
    str | None,
    "transfer_token",
    mcp=(
        "An ingest that fails after redeeming the token leaves it redeemable "
        "within its window, so a retry costs no second upload."
    ),
)

_INGEST_DRY_RUN = _ingest_param(
    bool,
    "dry_run",
    mcp=(
        "The source is located and hashed where it stands. Every validator "
        "that runs raises the error a real run would, except "
        "that a duplicate comes back as would_create: false rather than as "
        "duplicate_content. Unchecked, because each turns on state only the "
        "real write reaches: tier3_unique_constraint_violation, "
        "force_reingest_path_mismatch, stale_chain_head, and a "
        "force_reingest_pin_mismatch for a store-resident source with no "
        "prior document record. A clean preview is not a promise that the "
        "real run commits. For an upload, the checks that need the bytes "
        "wait for the call repeated with the transfer token."
    ),
)

# Maintenance-surface parameters. Each takes the text of the request field the
# REST operation validates, where one exists.
_RESTORE_SOURCE = model_param(
    str | None, SourceFileRestoreRequest, "source", mcp=_UPLOAD_RECIPE_LIFETIME
)
_RESTORE_TRANSFER_TOKEN = model_param(
    str | None,
    SourceFileRestoreRequest,
    "transfer_token",
    mcp=(
        "A repair that fails after redeeming the token leaves it redeemable "
        "within its window, so a retry costs no second upload."
    ),
)
_RESTORE_DOCUMENT_ID = model_param(str | None, SourceFileRestoreRequest, "document_id")
_RESTORE_SHA256 = model_param(
    str | None,
    SourceFileRestoreRequest,
    "sha256",
    mcp=(
        "A token seen by anyone not already holding the exact file therefore "
        "admits nothing. Pass it again on the completion call."
    ),
)
_INTEGRITY_CHECK_HASHES = model_param(bool, SourceFileIntegrityRequest, "check_hashes")
_INTEGRITY_DOCUMENT_IDS = model_param(list[str] | None, SourceFileIntegrityRequest, "document_ids")
_REABSTRACT_INCLUDE_PDF = model_param(bool, ReabstractRequest, "include_pdf")
_OPTIMIZE_CLEANUP_DAYS = model_param(int, OptimizeContentStoreRequest, "cleanup_older_than_days")
_EXPORT_OUTPUT_PATH = model_param(str, ExportProjectionRequest, "output_path")
_EXPORT_DOCUMENT_ID = shaped_param(
    str, DocumentIdStr, "Document whose stored projection is exported."
)
_NEW_VAULT_ID = shaped_param(
    str,
    VaultIdStr,
    (
        "Identifier the new vault would carry. It shapes the storage and "
        "brain roots in the returned scaffold; no vault with it need exist."
    ),
)
_CREATE_VAULT_CONFIG = model_param(
    dict,
    CreateVaultRequest,
    "config",
    mcp=(
        "Required sections: vault, document_types, lifecycle, "
        "metadata_extraction, edge_inference; `get_default_vault_config` "
        "serves a scaffold to start from."
    ),
)


def _config_section(field: str) -> object:
    """An ``update_vault_config`` section argument, described by its request field."""
    return model_param(dict | None, UpdateVaultConfigRequest, field)


_UPDATE_CONFIG_DRY_RUN = model_param(bool, UpdateVaultConfigRequest, "dry_run")
_UPDATE_CONFIG_FORCE = Annotated[
    bool,
    Field(
        description=(
            "When true, proceed with destructive changes; warnings are returned "
            "in the response. When false (default), destructive changes are "
            "refused with destructive_config_change (409)."
        )
    ),
]


def _collect_misplaced(keys: tuple[str, ...], supplied: dict[str, object]) -> list[str]:
    """Return the recognized keys that arrived as top-level arguments.

    ``supplied`` maps each recognized key to the value passed at the top
    level. Every non-None entry is collected, in canonical key order, so a
    caller who misplaced several fields repairs them in one round-trip
    instead of discovering them one error at a time -- and so the reported
    order is stable rather than varying with client argument order.
    """
    return [key for key in keys if supplied.get(key) is not None]


def _check_misplaced_metadata(supplied: dict[str, object]) -> None:
    """Raise ``MisplacedMetadataError`` when metadata fields arrive at the top level."""
    misplaced = _collect_misplaced(_INGEST_METADATA_KEYS, supplied)
    if misplaced:
        raise MisplacedMetadataError(
            fields=misplaced,
            recognized=list(_INGEST_METADATA_KEYS),
            example=_MISPLACED_METADATA_EXAMPLE,
        )


def _check_misplaced_filters(supplied: dict[str, object]) -> None:
    """Raise ``MisplacedFilterError`` when filter keys arrive at the top level."""
    misplaced = _collect_misplaced(_SEARCH_FILTER_KEYS, supplied)
    if misplaced:
        raise MisplacedFilterError(
            fields=misplaced,
            recognized=list(_SEARCH_FILTER_KEYS),
            example=_MISPLACED_FILTERS_EXAMPLE,
        )


def _validated_items(model: type[BaseModel], items: list) -> list:
    """Validate each item of a batch against its model, naming what refused.

    The whole batch is checked before the vault is opened, so a malformed item
    refuses the call without any of it being committed. What this adds over a
    bare comprehension is the refusal a caller reads: the item model is handed
    to the envelope, so a key the item does not declare comes back with the
    field set it could have used, and the location carries the item's position
    the way the HTTP surface reports it. One mistake, one refusal, whichever
    surface it was made on.

    Other failures keep the envelope and the location they already had. Only
    an undeclared key is relocated, because only it is built here rather than
    by the item's own validator. Its note that other objects carry undeclared
    keys looks past the failing item to the rest of the batch, as the HTTP
    surface's whole-body validation does.
    """
    validated = []
    for index, item in enumerate(items):
        try:
            validated.append(model.model_validate(item))
        except ValidationError as exc:
            envelope = validation_error_envelope(exc, root_model=model)
            if isinstance(envelope, UndeclaredKeyError):
                located = envelope.detail["parameter"]
                raise UndeclaredKeyError(
                    parameter=f"items.{index}" + (f".{located}" if located else ""),
                    keys=envelope.detail["keys"],
                    recognized=envelope.detail["recognized"],
                    elsewhere=envelope.elsewhere
                    or any(_carries_undeclared_key(model, later) for later in items[index + 1 :]),
                    aliases=envelope.detail.get("aliases"),
                    see_also=envelope.detail.get("see_also"),
                ) from exc
            raise
    return validated


def _carries_undeclared_key(model: type[BaseModel], item: object) -> bool:
    """Whether validating ``item`` against ``model`` refuses an undeclared key."""
    try:
        model.model_validate(item)
    except ValidationError as exc:
        return isinstance(validation_error_envelope(exc, root_model=model), UndeclaredKeyError)
    return False


def register_sage_tools(
    mcp: FastMCP,
    get_vault: Callable[[str], SAGEServices],
    serialize: Callable[[object], dict],
    error_response: Callable[[SAGEError | ValueError], dict],
    get_vault_registry_service: Callable[[], VaultRegistryService],
) -> dict[str, Callable]:
    """Register all SAGE protocol and API tools on the MCP server.

    Returns a dict mapping tool function names to the actual functions,
    for re-export from mcp_server.

    ``get_vault_registry_service`` is a call-time getter rather than an
    instance argument because the registration site (``sage.mcp_server``) is
    exposed to ``importlib.reload`` by ``tests/sage/test_cleanup_refactor.py``.
    After a reload, the module rebinds ``_vaults`` and
    ``_vault_registry_service`` to the original instances so that other
    modules keep working; resolving the service via the getter at call time
    picks up the rebound original, whereas capturing the instance at
    registration time would freeze the closures on the orphan reload-time
    object.
    """

    # -------------------------------------------------------------------
    # SAGE protocol tools
    # -------------------------------------------------------------------

    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    async def ingest_document(
        vault_id: VaultIdParam,
        source: _INGEST_SOURCE = None,
        source_type: _INGEST_SOURCE_TYPE = None,
        config: _INGEST_CONFIG = None,
        created_by: _ingest_param(
            str | None, "created_by", mcp="Without authentication, defaults to vault owner."
        ) = None,
        agent: _ingest_param(str | None, "agent") = None,
        force: _ingest_param(bool, "force") = False,
        predecessor_id: _INGEST_PREDECESSOR_ID = None,
        expected_head_version: _INGEST_EXPECTED_HEAD_VERSION = None,
        needs_review: _INGEST_NEEDS_REVIEW = False,
        metadata: _INGEST_METADATA = None,
        tier3_metadata: _INGEST_TIER3_METADATA = None,
        relocated_from: _INGEST_RELOCATED_FROM = None,
        document_id: _ingest_param(str | None, "document_id") = None,
        transfer_token: _INGEST_TRANSFER_TOKEN = None,
        dry_run: _INGEST_DRY_RUN = False,
        sha256: _ingest_param(
            str | None, "sha256", mcp="Pass it again on the completion call."
        ) = None,
        # Tripwires, not functional arguments. These are the ``metadata``
        # keys; they are published here only so a wrong-level spelling
        # reaches the guard instead of being stripped client-side. See
        # ``_INGEST_TRIPWIRE`` for what the shared annotation carries and
        # why each half of it is load-bearing.
        title: _INGEST_TRIPWIRE = None,
        version_label: _INGEST_TRIPWIRE = None,
        project: _INGEST_TRIPWIRE = None,
        doc_type: _INGEST_TRIPWIRE = None,
        authority_scope: _INGEST_TRIPWIRE = None,
        document_date: _INGEST_TRIPWIRE = None,
        tags: _INGEST_TRIPWIRE = None,
    ) -> dict:
        """Ingest a source file into SAGE, running the projection ->
        indexing -> abstraction pipeline.

        Stages 2-3 (indexing, abstraction) run in the background, so the
        call returns with ``pipeline_status`` typically non-terminal; a
        requested supersede runs synchronously, so the version chain is
        complete on return. To observe the outcome, wait for a terminal
        ``pipeline_status`` on the document: ``abstraction_complete``,
        ``abstraction_skipped``, or ``failed`` (which sets ``pipeline_error``).
        A wait must also accept
        ``abstraction_interrupted``: the work was stopped before it finished
        and the next server start re-runs it. Run one bounded wait on the
        caller's side, not a status request per unit of work.

        Metadata is caller-authoritative. ``dry_run=true`` previews the
        call and the doc_type's requirements, persisting nothing.

        Error modes:
        - ``vault_not_found`` (404)
        - 400: ``invalid_vault_id``, ``invalid_document_id``,
          ``invalid_document_date``, ``invalid_sha256``, ``misplaced_metadata``,
          ``misplaced_top_level_field``, ``undeclared_key``,
          ``source_type_unresolved``, ``adapter_not_found``,
          ``adapter_config_invalid``, ``source_unreadable``,
          ``source_digest_mismatch``, ``vault_source_path_refused``,
          ``relocated_from_provenance_mismatch``,
          ``relocation_source_undelivered``, ``ambiguous_ingest_source``,
          ``missing_ingest_source``,
          ``expected_head_version_requires_predecessor``, ``invalid_doc_type``,
          ``tier3_schema_violation``
        - 404: ``source_file_not_found``, ``document_not_found``
        - 409: ``duplicate_content``, ``force_reingest_path_mismatch``,
          ``force_reingest_pin_mismatch``, ``lifecycle_state_not_applicable``,
          ``supersede_target_not_active``, ``identical_content_supersede``,
          ``stale_chain_head``, ``tier3_unique_constraint_violation``,
          ``transfer_not_staged``, ``reserved_transition``,
          ``vault_migration_in_flight``
        - 410: ``transfer_token_invalid``
        - 422: ``invalid_parameter``
        - 500: ``transfer_endpoint_not_configured``
        - 502 / 503: ``vault_source_store_refused`` / ``vault_source_store_unavailable``
        """
        try:
            # First, before any validation or vault work: a misplaced
            # metadata field must not produce partial state. Rejecting here
            # guarantees the call is a no-op.
            _check_misplaced_metadata(
                {
                    "title": title,
                    "version_label": version_label,
                    "project": project,
                    "doc_type": doc_type,
                    "authority_scope": authority_scope,
                    "document_date": document_date,
                    "tags": tags,
                }
            )
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            if predecessor_id is not None:
                predecessor_id = _DOCUMENT_ID_ADAPTER.validate_python(predecessor_id)
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if sha256 is not None:
                sha256 = _SHA256_ADAPTER.validate_python(sha256)
            v = get_vault(vault_id)

            def _build_request(resolved_source: str) -> IngestRequest:
                # The model is named to the envelope, as the HTTP handler names
                # it for the same request. Without it a key nested inside an
                # argument -- ``relocated_from`` is the one that has any --
                # comes back as a complaint about a missing field of the object
                # the caller misspelled into, naming neither the key nor what
                # the object accepts, while the same call over HTTP names both.
                try:
                    return IngestRequest(
                        source=resolved_source,
                        source_type=source_type,
                        config=config,
                        created_by=created_by,
                        agent=agent,
                        force=force,
                        predecessor_id=predecessor_id,
                        expected_head_version=expected_head_version,
                        needs_review=needs_review,
                        metadata=metadata,
                        tier3_metadata=tier3_metadata,
                        relocated_from=relocated_from,
                        document_id=document_id,
                        dry_run=dry_run,
                        sha256=sha256,
                    )
                except ValidationError as exc:
                    raise validation_error_envelope(exc, root_model=IngestRequest) from exc

            # The delivery gate runs beneath the tool, in the service, so this
            # surface and the HTTP one reach the caller-local transfer on the
            # same terms. An omitted source_type is inferred beneath it too,
            # from the path the gate resolves.
            #
            # Fire-and-forget pipeline keeps this RPC under the 60s MCP client
            # timeout (BH-130). Callers wait for a terminal pipeline_status on
            # the document rather than requesting status per unit of work.
            result = await v.ingestion_service.ingest_from_caller(
                DeliveryDeclaration(source=source, transfer_token=transfer_token, sha256=sha256),
                _build_request,
                dry_run=dry_run,
                wait_for_pipeline=False,
            )
            if isinstance(result, (UploadRecipe, IngestPreview)):
                return serialize(result)
            payload = serialize(result.document)
            if result.warnings:
                payload["warnings"] = result.warnings
            return payload
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=READ_ONLY)
    async def get_filename_metadata(
        vault_id: VaultIdParam,
        filename: model_param(str, ParseFilenameRequest, "filename"),
        source_type: model_param(
            str,
            ParseFilenameRequest,
            "source_type",
            mcp=(
                f"The registered formats are {_REGISTERED_SOURCE_FORMATS} "
                "-- the same set `ingest_document` accepts, so a filename that "
                "parses here is one that can go on to be ingested."
            ),
        ),
    ) -> dict:
        """Parse a filename's basename through the vault's filename parsing and
        return the metadata fields the parser extracts from the supplied
        filename under the named adapter. Side-effect free: no document is
        created and vault state is unchanged.

        This is the companion to ``ingest_document``'s caller-authoritative
        metadata flow: call it first for filename-derived suggestions, decide
        which fields to keep, then call ``ingest_document`` with
        ``metadata=...``. Fields the parser could not extract come back null;
        when the vault has no ``filename_extraction.pattern`` configured, all
        fields are null.

        Which fields the parser extracts is vault-config-defined; see
        ``metadata_extraction.filename_extraction.segment_fields`` in
        ``get_vault_config``. In the ``cas`` vault the pattern is
        ``{date}_{project}_{code}_{title}_{version}``, so the parser returns
        ``doc_date``, ``project``, ``doc_code``, ``title``, and ``version``.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``adapter_not_found`` (400): no source adapter is registered for
          ``source_type``.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            source_type_enum = SourceType(source_type)
            response = v.ingestion_service.parse_filename(filename, source_type_enum)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=READ_ONLY)
    async def get_document(
        vault_id: VaultIdParam,
        document_id: shaped_param(
            str | None,
            DocumentIdStr,
            f"The document's unique identifier. {DOC_ID_ALIAS_NOTE}",
        ) = None,
        include_content: Annotated[
            bool,
            Field(
                description=(
                    "When true, add `content` (base64 whatever `body_form` "
                    "reports) and `content_size` to the response. Fails with "
                    "content_too_large (413) past the inline ceiling (default "
                    "100 MB; override via SAGE_MAX_INLINE_CONTENT_BYTES): use "
                    "`write_to_path` instead. Mutually exclusive with "
                    "`write_to_path`."
                )
            ),
        ] = False,
        write_to_path: Annotated[
            str | None,
            Field(description=_GET_DOCUMENT_WRITE_TO_PATH),
        ] = None,
        doc_id: DocIdAliasParam = None,
    ) -> dict:
        """Retrieve a document record with all metadata, lifecycle state, and
        pipeline status. Optional delivery of the vault-local source file
        bytes supports the agentic read-modify-reingest round-trip.

        Two mutually-exclusive delivery modes:
        - include_content=true: inline base64 bytes in the response.
          Best for small files.
        - write_to_path=/abs/path: SAGE writes the bytes to the
          filesystem path; response carries only metadata (written_to,
          content_size, content_hash). Preferred for files that would
          exceed MCP tool-result size ceilings.

        Write provenance, for the creation and the most recent modification:
        ``created_by`` / ``last_modified_by`` key the principal stably and
        ``created_client`` / ``last_modified_client`` name the client
        application, both server-derived from the validated token where the
        request authenticated; ``created_by_name`` /
        ``last_modified_by_name`` snapshot the user's display name, never a
        key; ``created_agent`` /
        ``last_modified_agent`` are the agent the caller asserted, marked
        ``trust: asserted`` and never verified.

        Error modes (the two store codes only when bytes are requested):
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``invalid_document_id`` (400)
        - ``document_not_found`` (404): no document with that id.
        - ``content_file_missing`` (404): bytes requested; the vault-local
          file is absent.
        - ``content_too_large`` (413)
        - ``content_delivery_conflict`` (400): both delivery modes set.
        - ``write_path_exists`` (409)
        - ``write_path_invalid`` (400)
        - ``vault_source_store_refused`` (502): resolve it at the store before
          retrying; ``detail.store_status`` carries its status.
        - ``vault_source_store_unavailable`` (503): the same call may succeed
          later.
        """
        try:
            # Validate each id-bearing parameter by its literal name (so the
            # typed-alias gate in tests/sage/test_typed_alias_coverage.py sees
            # a _DOCUMENT_ID_ADAPTER.validate_python(<param>) call for each),
            # then resolve the alias to one value, then raise the
            # ambiguous/missing errors before any service call.
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if doc_id is not None:
                doc_id = _DOCUMENT_ID_ADAPTER.validate_python(doc_id)
            if document_id is not None and doc_id is not None:
                raise AmbiguousDocumentIdentifierError(
                    tool="get_document", canonical="document_id", alias="doc_id"
                )
            if document_id is None and doc_id is None:
                raise MissingDocumentIdentifierError(
                    tool="get_document", accepted=["document_id", "doc_id"]
                )
            resolved_document_id = document_id if document_id is not None else doc_id
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            response = await v.documents_service.get_document_with_content(
                resolved_document_id, include_content, write_to_path
            )
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    async def update_lifecycles(
        vault_id: VaultIdParam,
        items: _LIFECYCLE_ITEMS,
        response_mode: model_param(
            str | None, BulkLifecycleRequest, "response_mode", mcp=_BATCH_RESPONSE_MODE_NOTE
        ) = None,
        dry_run: model_param(bool, BulkLifecycleRequest, "dry_run") = False,
        agent: model_param(str | None, BulkLifecycleRequest, "agent") = None,
    ) -> dict:
        """Apply one or more lifecycle state transitions to documents.

        Accepts ``items`` as a list of N>=1 per-item transition requests;
        ``items=[{...}]`` is the single-transition form. This is the sole
        MCP entry point for lifecycle transitions.

        The ``action`` vocabulary is vault-config-defined, not a fixed
        SAGE-wide set. Call with ``dry_run=true`` to learn it without
        writing: an action the vault does not offer the document's doc_type
        comes back as ``invalid_action`` carrying ``known_actions``, and a
        known action illegal from the document's current state comes back as
        ``invalid_lifecycle_transition`` carrying ``valid_actions``. For the
        full (from_state, action, to_state, creates_edge, doc_types) table
        rather than either answer, read ``lifecycle.transitions`` in the
        vault config via ``get_vault_config``.

        **``supersede`` is the canonical atomic form for replacing one
        document with another:** it transitions the predecessor AND creates
        the ``supersedes`` edge (new -> old) in one operation. ``relocate``
        is the origin half of a move to another vault.

        A non-terminal ``pipeline_status`` is not a refusal, on ``complete``
        or on any other action: the transition applies and the item's
        ``warnings`` carries the pipeline-still-in-progress advisory.

        **The batch is NOT atomic.** Inspect each
        ``BulkLifecycleItemResult.status`` and the aggregate
        ``success_count`` / ``error_count``. Empty ``items`` is valid: empty
        ``results``, all counts zero.

        Per-item error modes (inside the response envelope):
        ``missing_document_identifier``, ``ambiguous_document_identifier``,
        ``document_not_found``, ``invalid_action``,
        ``invalid_lifecycle_transition``, ``missing_successor_id``,
        ``missing_relocated_to``, ``unexpected_successor_id``,
        ``unexpected_relocated_to``, ``relocated_to_provenance_mismatch``,
        ``reserved_transition``.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``undeclared_key`` (400): ``detail.parameter`` locates the object
        - ``invalid_document_id`` (400)
        - ``invalid_sha256`` (400)
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            # Up-front shape validation across the whole batch: rejecting
            # the request here (rather than per-item inside the service
            # loop) guarantees that a malformed item produces an error
            # envelope without committing any partial state. The
            # ``response_mode`` ValueError from Pydantic enum validation
            # rides this same up-front rejection path.
            validated_items = _validated_items(BATCH_ITEM_MODELS["update_lifecycles"], items)
            v = get_vault(vault_id)
            request = BulkLifecycleRequest(
                agent=agent,
                items=validated_items,
                response_mode=response_mode,
                dry_run=dry_run,
            )
            response = await v.lifecycle_service.bulk_set_lifecycle(request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=WRITE_ADDITIVE)
    async def create_edges(
        vault_id: VaultIdParam,
        items: _LINK_ITEMS,
        response_mode: model_param(
            str | None, BulkLinkRequest, "response_mode", mcp=_BATCH_RESPONSE_MODE_NOTE
        ) = None,
        dry_run: model_param(bool, BulkLinkRequest, "dry_run") = False,
        agent: model_param(str | None, BulkLinkRequest, "agent") = None,
    ) -> dict:
        """Create one or more typed edges between documents in the graph.

        ``items=[{...}]`` is the single-edge form; this is the sole MCP entry
        point for edge creation. Dispatch is idempotent: a duplicate
        natural-key triple (``source_id``, ``target_id``, ``edge_type``)
        returns the existing edge with ``created=false``.

        **For ``supersedes`` edges, prefer ``update_lifecycles`` with
        ``action="supersede"``** (or ``ingest_document(..., predecessor_id=...)``):
        those transition the predecessor too. Use this only to stitch a
        missing edge into a chain whose lifecycle states are already correct.

        **A ``depends_on`` edge is evaluated later, not when it is
        created;** ``verify_preconditions`` evaluates it. With no
        configuration the dependency-satisfying set is the engine default,
        ``active`` and ``completed``, so a target that is still open
        satisfies the dependency: ``depends_on`` means the target exists and
        is live, not that it is finished. A vault opts a base state out by
        declaring ``satisfies_dependency: false`` on it in its lifecycle
        configuration, and opts a domain state in with
        ``satisfies_dependency: true``. To make ``depends_on`` mean blocked
        until the target is complete, declare ``satisfies_dependency:
        false`` on ``active``; ``completed`` then remains the only
        satisfying base state.

        **The batch is NOT atomic.** Inspect each
        ``BulkLinkItemResult.status`` and ``success_count`` /
        ``error_count``. Empty ``items`` is valid: empty ``results``, all
        counts zero.

        Per-item error modes (inside the response envelope):
        ``self_referential_edge`` (400), ``document_not_found`` (404),
        ``tbd_policy_edge`` (400), ``edge_anchor_policy_violation`` (400),
        ``retract_target_not_edge`` (400), ``merged_from_validation`` (400),
        ``synced_from_inapplicable_edge_type`` (400),
        ``synced_from_version_not_in_source_chain`` (404).

        Error modes:
        - ``vault_not_found`` (404)
        - 400: ``invalid_vault_id``, ``undeclared_key``, ``invalid_sha256``,
          ``invalid_document_id``, ``invalid_edge_id``
        - ``invalid_parameter`` (422): a malformed item or ``response_mode``
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            # Up-front shape validation across the whole batch: rejecting
            # the request here (rather than per-item inside the service
            # loop) guarantees that a malformed item produces an error
            # envelope without committing any partial state. The
            # ``response_mode`` ValueError from Pydantic enum validation
            # rides this same up-front rejection path.
            validated_items = _validated_items(BATCH_ITEM_MODELS["create_edges"], items)
            v = get_vault(vault_id)
            request = BulkLinkRequest(
                agent=agent,
                items=validated_items,
                response_mode=response_mode,
                dry_run=dry_run,
            )
            response = await v.graph_ops_service.create_edges(request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    async def update_metadata(
        vault_id: VaultIdParam,
        items: _METADATA_ITEMS,
        response_mode: model_param(
            str | None, BulkMetadataRequest, "response_mode", mcp=_BATCH_RESPONSE_MODE_NOTE
        ) = None,
        dry_run: model_param(bool, BulkMetadataRequest, "dry_run") = False,
        agent: model_param(str | None, BulkMetadataRequest, "agent") = None,
    ) -> dict:
        """Patch mutable metadata fields on one or more documents.

        Accepts ``items`` as a list of N>=1 per-item patch requests;
        ``items=[{...}]`` is the single-document form. This is the sole MCP
        entry point for metadata patching.

        Scalars use set-or-omit semantics: pass to set, omit to leave
        unchanged.

        Each successful per-item patch sets ``metadata_confirmed=true``; an
        item carrying only ``document_id`` is a pure-confirmation flip, not a
        no-op. The ``doc_type`` value must be one this vault declares. Call
        with ``dry_run=true`` to learn which without writing: an undeclared
        value comes back as ``invalid_doc_type`` carrying ``valid_types``,
        the whole vocabulary, and a payload that fails a declared doc_type's
        typed-metadata schema comes back as ``tier3_schema_violation``
        carrying ``requirements``.

        **The batch is NOT atomic.** A per-item error surfaces in that
        item's error envelope without rolling back other items; inspect each
        ``BulkMetadataItemResult.status`` and ``success_count`` /
        ``error_count``. Empty ``items`` is valid: empty ``results``, all
        counts zero.

        Per-item error modes (inside the response envelope):
        ``document_not_found`` (404), ``invalid_doc_type`` (400),
        ``{field}_add_conflict`` / ``{field}_remove_conflict`` (400),
        ``tag_patch_overlap``, ``tier3_unset_conflict``,
        ``tier3_patch_overlap``, ``patch_empty`` (400),
        ``tier3_schema_violation`` (400),
        ``tier3_doc_type_change_stale_keys`` (400),
        ``lifecycle_state_not_applicable`` (409), ``stale_read`` (409).

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - 400: ``legacy_form``, ``invalid_document_id``, ``invalid_document_date``
        - ``undeclared_key`` (400): ``lifecycle_status`` is set with ``update_lifecycles``
        - ``invalid_parameter`` (422): a malformed item or ``response_mode``
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            # Up-front per-item legacy-form check BEFORE Pydantic
            # validation: without this, Pydantic would raise a generic
            # ValueError for a bare-list `tags` or bare-dict
            # `tier3_metadata` value and the `legacy_form` error code
            # (with the worked-example detail per CAS-ADR-028) would be
            # lost to the caller.
            for item in items:
                if isinstance(item, dict):
                    _check_legacy_patch_form("tags", item.get("tags"))
                    _check_legacy_patch_form("tier3_metadata", item.get("tier3_metadata"))
            # Up-front shape validation across the whole batch: rejecting
            # the request here (rather than per-item inside the service
            # loop) guarantees that a malformed item produces an error
            # envelope without committing any partial state. The
            # ``response_mode`` ValueError from Pydantic enum validation
            # rides this same up-front rejection path.
            validated_items = _validated_items(BATCH_ITEM_MODELS["update_metadata"], items)
            v = get_vault(vault_id)
            request = BulkMetadataRequest(
                agent=agent,
                items=validated_items,
                response_mode=response_mode,
                dry_run=dry_run,
            )
            response = await v.metadata_service.bulk_update_metadata(request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    async def delete_edge(
        vault_id: VaultIdParam,
        edge_id: shaped_param(
            str,
            EdgeIdStr,
            'Production edge identifier, as ``search`` with target="edges" returns it.',
        ),
        dry_run: Annotated[
            bool,
            Field(
                description=(
                    "Confirm the edge exists and preview the deletion without persisting: "
                    "the response carries ``deleted=false``, ``dry_run=true`` and the edge "
                    "in ``preview_edge``. Default false."
                )
            ),
        ] = False,
    ) -> dict:
        """Delete a production edge from the graph.

        For staging-table edges (pre-confirmation), use
        ``update_staging_edge(action="dismiss")`` instead — the two tables
        are distinct and edge ids do not cross between them.

        Discovering ``edge_id``: use ``search`` with ``target="edges"`` to
        enumerate production edges by ``source_id`` / ``target_id`` /
        ``edge_type``, e.g. ``search(vault_id=..., mode="catalog",
        target="edges", filters={"source_id": "...", "edge_type": "..."})``.
        The returned ``edge_id`` is the value to pass here.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``invalid_edge_id`` (400)
        - ``edge_not_found`` (404): raised on a dry run too
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            edge_id = _EDGE_ID_ADAPTER.validate_python(edge_id)
            v = get_vault(vault_id)
            result = await v.graph_ops_service.unlink(edge_id, dry_run=dry_run)
            return serialize(result)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=READ_ONLY)
    async def verify_preconditions(
        vault_id: VaultIdParam,
        document_id: shaped_param(
            str,
            DocumentIdStr,
            (
                "Identifier of the document whose preconditions to check. "
                "Any document with outbound `depends_on` edges is valid."
            ),
        ),
    ) -> dict:
        """Check whether all depends_on targets for a document are
        satisfied (dependency-satisfying lifecycle, pipeline not failed).

        Iterates the document's outbound ``depends_on`` edges; for each
        target, verifies the lifecycle status is one the vault's
        configuration declares dependency-satisfying and pipeline_status
        not ``failed`` — a target still mid-pipeline is not rejected. A
        target the vault does not hold is reported unsatisfied with
        ``actual`` of "not found" rather than raising. Each row names its
        target by ``title`` and ``doc_type`` as well as ``target_id``, both
        null when the target is not found.
        Returns ``satisfied`` boolean plus a
        per-edge breakdown of failing reasons (e.g. predecessor still
        in projection, target archived) so the caller can act on the
        gap rather than re-querying each dependency.

        With no configuration the dependency-satisfying set is the engine
        default, ``active`` and ``completed``, so a target that is still
        open satisfies the dependency: ``depends_on`` means the target
        exists and is live, not that it is finished. A vault opts a base
        state out by declaring ``satisfies_dependency: false`` on it in its
        lifecycle configuration, and opts a domain state in with
        ``satisfies_dependency: true``. To make ``depends_on`` mean blocked
        until the target is complete, declare ``satisfies_dependency:
        false`` on ``active``; ``completed`` then remains the only
        satisfying base state. Satisfaction belongs to the state alone: a
        state whose ``doc_types`` scopes it to some doc_types satisfies,
        or not, exactly as it would unscoped, whatever the doc_type of
        the document that depends on it.

        This is not a mutation preview. A ``dry_run`` on a mutation
        answers what that one call would do to committed state; this
        answers whether a document's dependencies are in a state that
        permits work to proceed, aggregated across every outbound
        ``depends_on`` edge.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``invalid_document_id`` (400)
        - ``document_not_found`` (404)
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            result = await v.graph_ops_service.check_preconditions(document_id)
            return serialize(result)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=READ_ONLY)
    async def traverse(
        vault_id: VaultIdParam,
        start_id: model_param(
            str | None,
            TraverseRequest,
            "start_id",
            mcp=(
                "Alias: `document_id`; supply exactly one of the two. The "
                "response key remains `start_id` whichever form was used."
            ),
        ) = None,
        edge_type: model_param(
            str | None, TraverseRequest, "edge_type", mcp=_EDGE_TYPE_NOTE
        ) = None,
        direction: model_param(str, TraverseRequest, "direction", mcp=_DIRECTION_NOTE) = (
            "outbound"
        ),
        depth: model_param(int, TraverseRequest, "depth", mcp=_DEPTH_NOTE) = 3,
        debug: model_param(
            bool,
            TraverseRequest,
            "debug",
            mcp=(
                "Entries are `anchor_hit`, `anchor_miss`, `retracts_applied` "
                "and `tombstone_applied`."
            ),
        ) = False,
        document_id: shaped_param(
            str | None,
            DocumentIdStr,
            (
                "Alias for `start_id`; supply exactly one of the two. "
                "Supplying both, even with equal values, returns "
                "ambiguous_document_identifier."
            ),
        ) = None,
    ) -> dict:
        """Walk the document graph from a starting document.

        Traversal honors chain-scoped edge resolution: anchor fields
        determine which edges are visible from the query version's lineage;
        `retracts` edges can suppress downstream edges; `merged_from`
        tombstones suppress predecessor-chain edges downstream of the
        termination point.

        **Outbound dedup on ``transitive_both`` edges.** For
        ``transitive_both`` edge types (``covers``, ``references``,
        ``bundles_with``, ``depends_on``, ``instantiated_from``), outbound
        traversal deduplicates by target document: at most one
        representative edge per (source-chain, target) pair is returned,
        chain-scoped resolution selecting the winner from the query
        position's lineage. Distinct edges from different chain members
        pointing at the same target are masked. To enumerate every edge into
        a target chain, traverse **inbound** from the target instead.
        ``edge_counts.{edge_type}`` reflects total visible edges from the
        query position including masked ones; if ``edge_counts >
        len(nodes)`` the result has masked siblings. ``supersedes`` is
        point-to-point and not subject to this dedup; the rule applies only
        to the five ``transitive_both`` types.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``invalid_document_id`` (400)
        """
        try:
            # Validate each id-bearing parameter by its literal
            # name (so the typed-alias conformance gate in
            # tests/sage/test_typed_alias_coverage.py sees a
            # _DOCUMENT_ID_ADAPTER.validate_python(<param>) call for
            # each), then resolve the alias to a single value, then
            # surface the tool-specific ambiguous/missing errors before
            # any service call so callers don't see a downstream
            # document_not_found for an empty input.
            if start_id is not None:
                start_id = _DOCUMENT_ID_ADAPTER.validate_python(start_id)
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if start_id is not None and document_id is not None:
                raise AmbiguousDocumentIdentifierError(
                    tool="traverse",
                    canonical="start_id",
                    alias="document_id",
                )
            if start_id is None and document_id is None:
                raise MissingDocumentIdentifierError(
                    tool="traverse",
                    accepted=["start_id", "document_id"],
                )
            resolved_start_id = start_id if start_id is not None else document_id
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            request = TraverseRequest(
                start_id=resolved_start_id,
                edge_type=edge_type,
                direction=direction,
                depth=depth,
                debug=debug,
            )
            response = await v.graph_ops_service.traverse(request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=READ_ONLY)
    async def chain(
        vault_id: VaultIdParam,
        edge_type: Annotated[
            str, Field(description=param_doc(ChainRequest, "edge_type", mcp=_EDGE_TYPE_NOTE))
        ] = ("supersedes"),
        document_id: shaped_param(
            str | None,
            DocumentIdStr,
            param_doc(
                ChainRequest,
                "document_id",
                mcp=(
                    "The result is symmetric: any chain member returns the full "
                    f"ordered chain with that member's position indicated. {DOC_ID_ALIAS_NOTE}"
                ),
            ),
        ) = None,
        doc_id: DocIdAliasParam = None,
        limit: Annotated[int | None, Field(description=param_doc(ChainRequest, "limit"))] = None,
        offset: Annotated[int, Field(description=param_doc(ChainRequest, "offset"))] = 0,
    ) -> dict:
        """Walk an edge chain to both ends from a starting document.

        Follows edges of a single type in both directions from the
        starting document, collecting all reachable nodes into an
        ordered list with positional metadata (head, tail, query
        position, ``is_linear``). Three round-trips for a chain of two
        or more: an existence check on the starting document, then the
        walk itself -- one recursive CTE whatever the chain length --
        then a query for the connecting edges. A chain whose
        ``total_length`` is 1 skips that edge query and spends two
        round-trips on the ``available_edge_types`` hint instead, asking
        for the document's inbound and outbound edges separately: four
        in all. The hint turns on the whole chain being one document,
        not on a page of one, so a ``limit`` of 1 over a longer chain
        does not trigger it. Designed for version history retrieval on
        supersedes chains but works with any edge type. A document with
        no edges of the requested type returns a single-entry chain (the
        document itself as both head and tail).

        ``limit`` and ``offset`` page the returned entries without
        changing the walk: ``total_length`` stays the size of the whole
        chain, ``length`` counts what this response carries, and each
        entry keeps its absolute ``position`` rather than being
        renumbered from the start of the page. ``head_id`` and
        ``tail_id`` likewise name the chain's own ends, which a page
        need not contain.

        ``is_linear`` is true when the chain is strictly linear: no
        document shares a predecessor or a successor of the requested
        edge type with another. False marks a fork or a merge in the
        lineage -- a data quality problem rather than an ordinary shape,
        and worth surfacing when chasing supersedes chains that have not
        yet been reconciled.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``invalid_document_id`` (400)
        - ``document_not_found`` (404)
        """
        try:
            # See get_document: validate each id param by literal name for the
            # typed-alias gate, then resolve the alias and surface the
            # ambiguous/missing errors before any service call.
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if doc_id is not None:
                doc_id = _DOCUMENT_ID_ADAPTER.validate_python(doc_id)
            if document_id is not None and doc_id is not None:
                raise AmbiguousDocumentIdentifierError(
                    tool="chain", canonical="document_id", alias="doc_id"
                )
            if document_id is None and doc_id is None:
                raise MissingDocumentIdentifierError(
                    tool="chain", accepted=["document_id", "doc_id"]
                )
            resolved_document_id = document_id if document_id is not None else doc_id
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            request = ChainRequest(
                document_id=resolved_document_id,
                edge_type=edge_type,
                limit=limit,
                offset=offset,
            )
            response = await v.graph_ops_service.chain(request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=READ_ONLY)
    async def search(
        vault_id: VaultIdParam,
        mode: _SEARCH_MODE = None,
        query: _SEARCH_QUERY = None,
        scope: _SEARCH_SCOPE = "all",
        filters: _SEARCH_FILTERS = None,
        document_id: _discover_param(str | None, "document_id") = None,
        heading_path: _discover_param(Annotated[str | None, NumericText], "heading_path") = None,
        limit: _SEARCH_LIMIT = 10,
        offset: _discover_param(int, "offset") = 0,
        use_hybrid: _discover_param(bool, "use_hybrid") = True,
        use_abstract_prefilter: _discover_param(bool, "use_abstract_prefilter") = True,
        include_abstracts: _discover_param(bool, "include_abstracts") = False,
        min_relevance: _discover_param(float | None, "min_relevance") = None,
        target: _discover_param(str, "target") = "documents",
        response_mode: _SEARCH_RESPONSE_MODE = None,
        sort_by: _SEARCH_SORT_BY = None,
        sort_order: _SEARCH_SORT_ORDER = None,
        facet_fields: _discover_param(list[FacetField] | None, "facet_fields") = None,
        facet_value_limit: _SEARCH_FACET_VALUE_LIMIT = None,
        # Tripwires, not functional arguments. These are the ``filters``
        # keys; they are published here only so a wrong-level spelling
        # reaches the guard instead of being stripped client-side. See
        # ``_SEARCH_TRIPWIRE`` for what the shared annotation carries and
        # why each half of it is load-bearing.
        doc_type: _SEARCH_TRIPWIRE = None,
        project: _SEARCH_TRIPWIRE = None,
        lifecycle_status: _SEARCH_TRIPWIRE = None,
        exclude_terminal_lifecycle: _SEARCH_TRIPWIRE = None,
        tags: _SEARCH_TRIPWIRE = None,
        document_ids: _SEARCH_TRIPWIRE = None,
        pipeline_status: _SEARCH_TRIPWIRE = None,
        source_type: _SEARCH_TRIPWIRE = None,
        tier3_metadata: _SEARCH_TRIPWIRE = None,
        provenance: _SEARCH_TRIPWIRE = None,
        source_id: _SEARCH_TRIPWIRE = None,
        target_id: _SEARCH_TRIPWIRE = None,
        edge_type: _SEARCH_TRIPWIRE = None,
    ) -> dict:
        """Search documents, edges, or facets; semantic, keyword, catalog, or deterministic modes.

        Modes (for the default ``target="documents"``):
            semantic: Vector + optional BM25 fusion. Requires query.
            keyword: BM25-only search. Requires query; ``query`` gives the syntax.
            catalog: Filter-only enumeration -- the canonical way to list the
                documents already in a vault. Metadata only, paged by limit +
                offset; ``limit=0`` returns ``total_available`` alone.
            deterministic: Exact heading-path extraction. Requires
                document_id + heading_path.

        Edge enumeration:
            ``target="edges"`` returns edge rows, with the id
            ``delete_edge`` needs, filtered by ``source_id``, ``target_id``,
            ``edge_type``. A call omitting ``mode`` resolves to catalog;
            naming a non-catalog mode is still refused. E.g.
            ``filters={"source_id": "<doc_id>", "edge_type": "references"}``.

        Facet enumeration:
            ``target="facets"`` returns a row per field (doc_type,
            lifecycle_status, source_type, pipeline_status, tags) with
            top values, counts and ``total_distinct``, capped by
            ``facet_value_limit``. A call omitting ``mode``
            resolves to catalog; naming a non-catalog mode is still refused.
            ``created_by``/``last_modified_by`` take a display name and are
            ``facet_fields``.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``misplaced_filters`` (400): a filter key given as a top-level argument
        - ``invalid_mode`` (400)
        - ``unknown_filter_key`` (400): a ``filters`` key that is not declared
        - ``invalid_filter_value`` (400): a value outside a closed vocabulary
        - ``invalid_filter_shape`` (400): a ``filters`` value of the wrong type
        - ``invalid_document_id`` (400): a malformed ``document_ids`` entry
        - ``mode_parameter_mismatch`` (400): a parameter the mode or target forbids
        - ``missing_query`` / ``missing_document_id`` / ``missing_heading_path`` (400)
        - ``invalid_parameter`` (422): a bound or type violation outside ``filters``
        - ``storage_query_failed`` (500): a defect to report
        """
        try:
            # First, before any validation or vault work: a misplaced filter
            # key must not reach the retrieval path, where it would be
            # missing from the constraints rather than named in an error.
            _check_misplaced_filters(
                {
                    "doc_type": doc_type,
                    "project": project,
                    "lifecycle_status": lifecycle_status,
                    "exclude_terminal_lifecycle": exclude_terminal_lifecycle,
                    "tags": tags,
                    "document_ids": document_ids,
                    "pipeline_status": pipeline_status,
                    "source_type": source_type,
                    "tier3_metadata": tier3_metadata,
                    "provenance": provenance,
                    "source_id": source_id,
                    "target_id": target_id,
                    "edge_type": edge_type,
                }
            )
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            # Pass the raw dict so DiscoverRequest performs the
            # nested RetrievalFilters validation. This keeps the
            # ValidationError loc prefixed with ``("filters", ...)``, which
            # the translator in sage.api.errors needs to map into typed
            # ``unknown_filter_key`` / ``invalid_filter_shape`` envelopes.
            request = DiscoverRequest(
                mode=mode,
                query=query,
                scope=scope,
                filters=filters,
                document_id=document_id,
                heading_path=heading_path,
                limit=limit,
                offset=offset,
                target=target,
                response_mode=response_mode,
                use_hybrid=use_hybrid,
                use_abstract_prefilter=use_abstract_prefilter,
                include_abstracts=include_abstracts,
                min_relevance=min_relevance,
                sort_by=sort_by,
                sort_order=sort_order,
                facet_fields=facet_fields,
                facet_value_limit=facet_value_limit,
            )
            response = await v.retrieval_service.discover(request)
            return serialize(response)
        except SAGEError as e:
            return error_response(e)
        except ValidationError as e:
            sage_err = translate_validation_error(e)
            if sage_err is not None:
                return error_response(sage_err)
            return error_response(e)
        except ValueError as e:
            return error_response(e)

    @mcp.tool(annotations=READ_ONLY)
    async def read_projection(
        vault_id: VaultIdParam,
        document_id: shaped_param(
            str | None,
            DocumentIdStr,
            f"The document's unique identifier. {DOC_ID_ALIAS_NOTE}",
        ) = None,
        write_to_path: Annotated[
            str | None,
            Field(description=_READ_PROJECTION_WRITE_TO_PATH),
        ] = None,
        doc_id: DocIdAliasParam = None,
        delivery: Annotated[
            Literal["inline", "spill", "auto"],
            Field(
                description=(
                    "Pins the delivery shape instead of leaving it implicit in "
                    "whether `write_to_path` was supplied. `auto` (default): "
                    "spill to disk when `write_to_path` is given, inline "
                    "otherwise. `inline`: force the inline body; supplying "
                    "`write_to_path` alongside it returns delivery_conflict. "
                    "`spill`: force write-to-disk delivery; it requires "
                    "`write_to_path` and returns delivery_conflict without one. "
                    "Decide up front with `read_meta.body_length` (the inline "
                    "body size on a prior or auto read) before forcing `inline` "
                    "on a large document."
                )
            ),
        ] = "auto",
    ) -> dict:
        """Read a document's full text into context with metadata header.

        Two delivery modes:
        - inline (default): returns the complete projection (reconstructed
          from stored chunks) inline as ``projection_text``, equivalent to
          uploading the document. Use this instead of ``search``
          when you need the whole document.
        - ``write_to_path=/abs/path``: SAGE writes the projection text
          to the given absolute path. Preferred for large projections that
          would exceed the MCP tool-result inline budget. Mirrors
          ``get_document(write_to_path=...)``.

        ``no_projection`` means the document exists but has no stored
        projection (e.g. ingestion failed mid-pipeline or the document is
        awaiting reabstraction). Inspect ``pipeline_status`` via
        ``get_document``; if recoverable, ``recompute_abstract`` may restore
        the projection.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``invalid_document_id`` (400)
        - ``document_not_found`` (404)
        - ``no_projection`` (404)
        - ``delivery_conflict`` (400): ``delivery`` contradicts
          ``write_to_path``.
        - ``write_path_exists`` (409)
        - ``write_path_invalid`` (400)
        """
        try:
            # See get_document: validate each id param by literal name for the
            # typed-alias gate, then resolve the alias and surface the
            # ambiguous/missing errors before any service call.
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if doc_id is not None:
                doc_id = _DOCUMENT_ID_ADAPTER.validate_python(doc_id)
            if document_id is not None and doc_id is not None:
                raise AmbiguousDocumentIdentifierError(
                    tool="read_projection", canonical="document_id", alias="doc_id"
                )
            if document_id is None and doc_id is None:
                raise MissingDocumentIdentifierError(
                    tool="read_projection", accepted=["document_id", "doc_id"]
                )
            resolved_document_id = document_id if document_id is not None else doc_id
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            response = await v.utilities_service.read_projection(
                resolved_document_id, write_to_path=write_to_path, delivery=delivery
            )
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=READ_ONLY)
    async def read_section(
        vault_id: VaultIdParam,
        heading_path: Annotated[
            str,
            NumericText,
            Field(
                description=(
                    'Heading path prefix, its headings joined by " > ". The '
                    "empty string addresses the text under no heading."
                )
            ),
        ],
        document_id: shaped_param(
            str | None,
            DocumentIdStr,
            f"The document's unique identifier. {DOC_ID_ALIAS_NOTE}",
        ) = None,
        doc_id: DocIdAliasParam = None,
    ) -> dict:
        """Read a section of a document by heading path.

        Returns clean readable text for a heading subtree without loading
        the full document. Uses structural prefix matching from the
        document root: requesting "Technical Description" returns that
        heading plus all children (e.g.
        "Technical Description > Composite Claim Binding"). Bare-text
        queries that match the *tail* of a stored path (e.g. "CLAIMS"
        against a stored "CLAIMS -- Remove Before Filing") will not match
        — when this happens, the heading_not_found error includes a
        ``candidate_matches`` field listing stored paths that contain the
        query as a substring, so you can retry with the exact path.

        For free-text "find this section by name regardless of path
        position," prefer search semantic or keyword mode — both
        index heading_path text alongside content.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``invalid_document_id`` (400)
        """
        try:
            # See get_document: validate each id param by literal name for the
            # typed-alias gate, then resolve the alias and surface the
            # ambiguous/missing errors before any service call.
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if doc_id is not None:
                doc_id = _DOCUMENT_ID_ADAPTER.validate_python(doc_id)
            if document_id is not None and doc_id is not None:
                raise AmbiguousDocumentIdentifierError(
                    tool="read_section", canonical="document_id", alias="doc_id"
                )
            if document_id is None and doc_id is None:
                raise MissingDocumentIdentifierError(
                    tool="read_section", accepted=["document_id", "doc_id"]
                )
            resolved_document_id = document_id if document_id is not None else doc_id
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            response = await v.utilities_service.read_section(resolved_document_id, heading_path)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=READ_ONLY)
    async def list_headings(
        vault_id: VaultIdParam,
        document_id: shaped_param(
            str | None,
            DocumentIdStr,
            f"The document's unique identifier. {DOC_ID_ALIAS_NOTE}",
        ) = None,
        doc_id: DocIdAliasParam = None,
    ) -> dict:
        """List all heading paths for a document in document order.

        Returns the distinct heading paths present in a document, ordered
        by their position in the source -- the structural table of
        contents. Body content is not read. Use this to verify a
        document's structure or pick a heading path before calling
        read_section.

        Replaces the antipattern of calling read_section with a
        deliberately wrong heading path to harvest ``available_headings``
        from the resulting ``heading_not_found`` error response.
        Every returned path is one a caller may pass to read_section.
        An authored heading is listed by its path. A heading with no text is
        not a heading and is not listed: the text under it is read in the
        section before it. The empty path, first
        where present, addresses the text under no heading: the text
        before the document's first heading, or the whole of a document
        that has none.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``invalid_document_id`` (400)
        """
        try:
            # See get_document: validate each id param by literal name for the
            # typed-alias gate, then resolve the alias and surface the
            # ambiguous/missing errors before any service call.
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if doc_id is not None:
                doc_id = _DOCUMENT_ID_ADAPTER.validate_python(doc_id)
            if document_id is not None and doc_id is not None:
                raise AmbiguousDocumentIdentifierError(
                    tool="list_headings", canonical="document_id", alias="doc_id"
                )
            if document_id is None and doc_id is None:
                raise MissingDocumentIdentifierError(
                    tool="list_headings", accepted=["document_id", "doc_id"]
                )
            resolved_document_id = document_id if document_id is not None else doc_id
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            response = await v.utilities_service.list_headings(resolved_document_id)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="recompute_views", annotations=WRITE_DESTRUCTIVE)
    async def recompute_views(vault_id: VaultIdParam) -> dict:
        """Regenerate browsable symlink views (by_doc_type/, by_lifecycle/)
        in the vault's storage root.

        Drops and recreates the symlink trees under the vault's
        ``storage_root``. Useful after bulk metadata updates that moved
        documents between doc_type or lifecycle buckets. The views are for
        human file-browser navigation; no SAGE tool consumes them.

        Documents whose ``doc_type`` is null are silently omitted from
        ``by_doc_type/`` (no ``<null>/`` bucket); ``by_lifecycle/`` never
        drops one. A document absent from ``by_doc_type/`` has its
        ``doc_type`` unset -- patch via ``update_metadata`` and re-call.

        Wipe-then-rebuild is NOT atomic: a mid-rebuild failure leaves
        ``views/`` partially regenerated with no rollback; recovery is a
        re-call once the cause is addressed. On an empty vault the wipe runs,
        ``views/`` is not recreated, and the response carries
        ``views_generated=0``.

        The views are browsable only by a caller that shares the server's
        filesystem. Under the cloud profile the regeneration is refused before
        the existing views are touched; ``search`` in catalog mode enumerates
        the same buckets by ``doc_type`` or ``lifecycle_status`` instead.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``caller_filesystem_unavailable`` (501): cloud profile; nothing is touched
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            response = await v.utilities_service.refresh_views()
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    # -------------------------------------------------------------------
    # SAGE API tools for CAS Application (MCP-001 through MCP-014)
    # -------------------------------------------------------------------

    @mcp.tool(name="list_vaults", annotations=READ_ONLY)
    async def list_vaults() -> dict:
        """Enumerate all configured vaults. No vault_id parameter -- operates
        across all registered vaults, so this is the tool to call first when
        choosing which vault a ``vault_id`` argument should name.

        Each entry carries ``id``, ``name``, ``description``, and
        ``document_count``. The count spans every lifecycle state, including
        archived predecessors -- it says how much a vault holds, not how much
        of it is current; ``get_vault_stats`` gives the per-state
        breakdown for one vault. A plausible-looking vault with a count of
        zero is empty and not worth a further call.
        """
        try:
            listing = await get_vault_registry_service().vault_list()
            return {
                "vaults": [
                    {
                        "id": s.id,
                        "name": s.name,
                        "description": s.description,
                        "document_count": s.document_count,
                    }
                    for s in listing.vaults
                ],
                "count": listing.count,
                "read_meta": serialize(listing.read_meta),
            }
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="create_vault", annotations=WRITE_ADDITIVE)
    async def create_vault(config: _CREATE_VAULT_CONFIG) -> dict:
        """Create a new vault and register it with the running SAGE instance.

        The config is validated against the vault config schema, directories
        are created, ``vault_config.yaml`` is written under the vault root
        (default ``~/sage_vaults/<vault_id>/``), services are initialized,
        and the vault is registered immediately (no restart). The full
        written config is echoed back so the caller can follow up with
        ``update_vault_config`` without a separate read.

        The new vault inherits the running process's abstraction provider;
        the config's ``abstraction`` section governs only enable/disable and
        per-vault parameters. A different provider requires a stack-config
        edit and process restart.

        A doc_type's ``metadata_schema`` is compiled at create time, not
        first ingest, so a malformed schema is refused here.

        Creation is not atomic: a mid-sequence failure can leave
        ``~/sage_vaults/{vault_id}/`` present with a partial yaml while the
        registry has no entry; recovery is to remove the directory and
        re-call.

        Error modes:
        - ``vault_already_exists`` (409)
        - ``vault_config_validation_error`` (400): the config, or a doc_type's
          ``metadata_schema``, fails validation
        """
        try:
            summary = await get_vault_registry_service().create_vault(
                CreateVaultRequest(config=config)
            )
            return {
                "vault_id": summary.id,
                "name": summary.name,
                "storage_root": config["vault"]["storage_root"],
                "config": config,
            }
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="get_vault_config", annotations=READ_ONLY)
    async def get_vault_config(vault_id: VaultIdParam) -> dict:
        """Return the full vault configuration as a dict.

        Section structure follows the schema: ``vault``, ``document_types``,
        ``lifecycle``, ``metadata_extraction``, ``edge_inference``,
        ``adapter_defaults``, ``abstraction``, ``retrieval_health``,
        ``timing``.

        This is the authoritative source for vault-config-defined
        vocabulary that other tools depend on. Read this when you need:

        - The valid ``action`` vocabulary for ``update_lifecycles``
          (under ``lifecycle.transitions``; each entry includes
          ``from_state``, ``action``, ``to_state``, ``creates_edge``, and
          ``doc_types`` when the entry applies only to those doc_types).
        - The valid ``doc_type`` values for ``update_metadata``
          or for filtering ``search`` (under
          ``document_types.doc_types``).
        - The per-adapter projection parameters this vault sets (under
          ``adapter_defaults``, keyed by source type). Which adapters
          exist is not vault configuration; read ``adapters`` on the
          vault summary for the process-wide set.
        - The filename-parsing pattern and segment fields used by
          ``get_filename_metadata`` (under
          ``metadata_extraction.filename_extraction``).
        - The edge inference tier assignments and inference rules
          relevant to ``list_staging_edges`` (under
          ``edge_inference.tier_assignments``).

        The returned dict is the live in-memory config; on-disk edits
        to ``vault_config.yaml`` are not picked up until
        ``reload_vault`` is called.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            services = get_vault(vault_id)
            return services.vault_config_service.get_config()
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="update_vault_config", annotations=WRITE_DESTRUCTIVE)
    async def update_vault_config(
        vault_id: VaultIdParam,
        vault: _config_section("vault") = None,
        document_types: _config_section("document_types") = None,
        lifecycle: _config_section("lifecycle") = None,
        adapter_defaults: _config_section("adapter_defaults") = None,
        metadata_extraction: _config_section("metadata_extraction") = None,
        edge_inference: _config_section("edge_inference") = None,
        abstraction: _config_section("abstraction") = None,
        retrieval_health: _config_section("retrieval_health") = None,
        force: _UPDATE_CONFIG_FORCE = False,
        dry_run: _UPDATE_CONFIG_DRY_RUN = False,
    ) -> dict:
        """Update vault configuration at the section level.

        Each non-null section argument replaces the corresponding top-level
        config section wholesale; sections left None are preserved.
        Partial-section merges are not supported — passing
        ``document_types={"doc_types": [...]}`` replaces the entire
        ``document_types`` section, so include every key you want to keep.

        If the merged config would remove a doc_type or lifecycle state that
        still has documents attached, the update is rejected with
        ``destructive_config_change`` and the affected counts in the detail.
        Pass ``force=True`` to proceed anyway; the warnings then appear in
        the success response. Changing ``vault.id`` is never permitted
        regardless of force — use ``create_vault`` for a new vault.

        The update writes to disk and updates the running config in place;
        subsequent calls see the new vocabulary immediately. The
        write-then-reload sequence is atomic: if any step raises, the yaml is
        rolled back to its pre-call bytes and the previous config keeps
        serving. ``reload_vault`` is needed only when an external process
        edited the yaml.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``destructive_config_change`` (409): see above
        - ``vault_config_validation_error`` (400): the merged config fails
          validation, changes ``vault.id``, or declares a retired section
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            services = get_vault(vault_id)
            body = UpdateVaultConfigRequest(
                vault=vault,
                document_types=document_types,
                lifecycle=lifecycle,
                adapter_defaults=adapter_defaults,
                metadata_extraction=metadata_extraction,
                edge_inference=edge_inference,
                abstraction=abstraction,
                retrieval_health=retrieval_health,
                dry_run=dry_run,
            )
            return serialize(
                await services.vault_config_service.update_config(vault_id, body, force)
            )
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="get_vault_stats", annotations=READ_ONLY)
    async def get_vault_stats(vault_id: VaultIdParam) -> dict:
        """Vault statistics and health indicators.

        Returns aggregate counts and health summaries for the vault,
        including total document count, counts per lifecycle state,
        counts per doc_type, and counts per pipeline_status. Inexpensive;
        safe to poll.

        The doc-scoped health indicators are an operator worklist, so
        they exclude documents in a terminal lifecycle state -- the
        states the vault's own config marks ``is_terminal``, so a vault
        that declares one of its own has it honoured. Such a document is
        excluded because remediating it is not work an operator needs to
        take up, not because nothing will touch it again: the automatic
        re-abstraction paths select on ``pipeline_status`` alone and may
        still reach it. ``pending_edge_count`` is the one exception and
        is reported unfiltered: a staging edge is not doc-scoped and
        carries no lifecycle state to exclude it by. The metadata-review
        queue read by ``list_pending_metadata`` is likewise unfiltered
        and keeps surfacing every unconfirmed document, so it can differ
        from ``pending_metadata_count``.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            services = get_vault(vault_id)
            stats = await services.vault_config_service.get_stats()
            return serialize(stats)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="verify_hashes", annotations=READ_ONLY)
    async def verify_hash(
        vault_id: VaultIdParam,
        hashes: model_param(
            list[str],
            HashCheckRequest,
            "hashes",
            mcp=(
                "Canonical `sha256:<hex>` or bare hex, digest in either case. "
                "An empty list short-circuits; a hash that cannot be "
                "normalized rejects the whole call with invalid_sha256."
            ),
        ),
    ) -> dict:
        """Bulk hash existence check against the graph store.

        For each input hash, returns whether an existing document in the
        vault carries that content hash and, if so, the matching document's
        ``document_id``. Used by the scan-and-batch-ingest flow to identify
        already-ingested files without re-hashing on the SAGE side. Where
        several documents carry one hash, the one named is a version a
        supersession has not retired, lowest document id among equals, so
        the same request names the same document every time.

        Matches on provenance: a document's ``source_content_hash`` is the
        SHA-256 of the bytes that were delivered at ingest, so the digest a
        caller computes over its own local file is the right thing to send
        here. That holds however the vault retains its copy -- a store that
        rewrites what it keeps records the rewritten digest separately, and
        this lookup does not consult it.

        Hash format: every hash is normalized to the canonical
        ``sha256:<64 lowercase hex>`` before lookup, so the canonical form,
        bare hex, and either with the digest uppercased all resolve to the
        same stored document. An uppercase algorithm prefix, a
        whitespace-padded value, a wrong-length digest, and a non-hex digest
        are rejected rather than normalized.

        Result shape: ``matches`` holds one entry per distinct *canonical*
        hash, carrying ``exists`` and, when matched, ``document_id``. No
        input is omitted — an unmatched hash is present with
        ``exists=false``. Because keys are canonical, two spellings of one
        digest collapse to a single entry, read back under the canonical key.

        Empty-list short-circuit: ``hashes=[]`` returns empty ``matches``
        without consulting the graph store. Since every non-empty input
        yields at least one entry, empty ``matches`` means the input was
        empty; it never means "nothing matched".

        Error modes:
        - ``invalid_sha256`` (400): names the offending value as supplied.
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            hashes = _SHA256_LIST_ADAPTER.validate_python(hashes)
            services = get_vault(vault_id)
            body = HashCheckRequest(hashes=hashes)
            return serialize(await services.vault_config_service.hash_check(body))
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=READ_ONLY)
    async def list_staging_edges(vault_id: VaultIdParam) -> dict:
        """List Tier 2 suggested edges awaiting review.

        SAGE's edge-inference subsystem runs edges through tiers
        defined in ``edge_inference.tier_assignments``:

        - **Tier 1** (e.g. ``supersedes`` via version_chain, ``sync_target``
          via re_ingestion): high-confidence inferences. Created
          directly as production edges; do not appear here.
        - **Tier 2** (e.g. ``references`` via identifier_mention or the
          superseded content_reference placeholder, ``covers`` via
          filename_code_match): inferred edges that require human review.
          Land in the staging-edge table; surfaced by this tool until
          confirmed or dismissed.
        - **Tier 3**: agent-supplied edges (``derived_from``,
          ``depends_on``); not inferred, so do not pass through staging.

        Each staging edge carries the source/target ids, edge_type,
        and the inference rule + evidence that produced it. The result
        list groups by source document
        (``staging_review_grouping=by_source_document``) so a reviewer
        can sweep all candidate edges from one document together.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            response = serialize(await v.staging_edges_service.list_staging_edges())
            return {
                **response,
                "vault_id": vault_id,
                "status": "awaiting_review" if response["items"] else "no_staging_edges",
            }
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    async def update_staging_edge(
        vault_id: VaultIdParam,
        edge_id: shaped_param(
            str,
            EdgeIdStr,
            "Staging edge identifier, from ``list_staging_edges``.",
        ),
        action: Annotated[
            str,
            Field(
                description=(
                    "One of "
                    + ", ".join(f'"{verb}"' for verb in STAGING_EDGE_ACTIONS)
                    + ". Any other value is refused with invalid_action."
                )
            ),
        ],
        agent: model_param(
            str | None,
            StagingEdgeConfirmRequest,
            "agent",
            mcp="A dismiss records nothing.",
        ) = None,
    ) -> dict:
        """Confirm or dismiss a staging edge.

        ``action="confirm"`` promotes the staging edge to production and
        returns the production ``edge_id``, distinct from the staging id
        passed in — staging and production tables do not share an id space.
        ``action="dismiss"`` deletes the staging edge without creating a
        production edge; a future re-ingest that re-triggers the inference
        rule will re-stage the candidate.

        Confirm is idempotent on the natural-key triple ``(source_id,
        target_id, edge_type)``: if production already holds that edge,
        confirm returns the existing production edge's id and consumes the
        staging row either way. Treat confirm as at-least-once: a retry that
        finds the staging row gone returns ``staging_edge_not_found``;
        ``search`` with ``target="edges"`` shows whether the production edge
        landed.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``invalid_edge_id`` (400)
        - ``staging_edge_not_found`` (404): already confirmed, already dismissed, or never existed
        - ``invalid_action`` (400): ``detail.known_actions`` names the accepted verbs
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            edge_id = _EDGE_ID_ADAPTER.validate_python(edge_id)
            if action not in STAGING_EDGE_ACTIONS:
                raise InvalidActionError(action, list(STAGING_EDGE_ACTIONS))
            try:
                request = StagingEdgeConfirmRequest(agent=agent)
            except ValidationError as exc:
                raise validation_error_envelope(exc, root_model=StagingEdgeConfirmRequest) from exc
            v = get_vault(vault_id)
            if action == "confirm":
                # Confirm inserts the production edge and then deletes the
                # staging row without one wrapping transaction. A delete that
                # fails after the insert leaves the staging row beside the new
                # edge until a later confirm consumes it, which the natural-key
                # idempotency above turns into a no-op.
                return serialize(
                    await v.staging_edges_service.confirm_staging_edge(edge_id, agent=request.agent)
                )
            return serialize(await v.staging_edges_service.dismiss_staging_edge(edge_id))
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=READ_ONLY)
    async def list_pending_metadata(
        vault_id: VaultIdParam,
        limit: Annotated[
            int,
            Field(
                description=(
                    f"Page size, 0..{PENDING_METADATA_MAX_LIMIT}. "
                    f"Default {PENDING_METADATA_DEFAULT_LIMIT}. "
                    "`0` returns `total_available` with no rows."
                )
            ),
        ] = PENDING_METADATA_DEFAULT_LIMIT,
        offset: Annotated[
            int, Field(description="Number of queue documents to skip before the page.")
        ] = 0,
        response_mode: Annotated[
            str | None,
            Field(
                description=(
                    f"{_RESPONSE_MODE_NOTE} Omitted, a page of more than five "
                    "rows is light and a smaller one is full."
                )
            ),
        ] = None,
    ) -> dict:
        """List documents with unconfirmed metadata.

        A document is "pending" when its ``metadata_confirmed`` flag is
        false. This typically arises from ``ingest_document(needs_review=true)``:
        the caller deferred metadata to filename inference, which populated
        omitted fields and held the document for review. The pending state is
        cleared on any ``update_metadata`` call against the document (even a
        single-field update).

        For the default ``ingest_document`` path (``needs_review=false``),
        documents land with ``metadata_confirmed=true`` and never appear
        here.

        This queue is deliberately not filtered by lifecycle state.
        Reviewing a retired document's metadata is legitimate, and this
        is the only surface that reaches it. ``pending_metadata_count``
        on ``get_vault_stats`` excludes documents in a terminal
        lifecycle state, so this list can be longer than that count.

        The queue is paged in document-id order with ``limit`` and
        ``offset``, and ``total_available`` counts the whole queue. Under
        ``response_mode=light`` each row is a ``DocumentSummaryLight``
        rather than a ``PendingMetadataItem``.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``invalid_parameter`` (422): ``limit``, ``offset`` or
          ``response_mode`` out of range.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            page = await v.metadata_service.list_pending_metadata(limit, offset, response_mode)
            return {
                **serialize(page),
                "count": len(page.items),
                "vault_id": vault_id,
                "status": "pending_review" if page.total_available else "no_pending_metadata",
            }
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    async def recompute_abstract(
        vault_id: VaultIdParam,
        document_id: shaped_param(
            str,
            DocumentIdStr,
            "Document to re-abstract.",
        ),
    ) -> dict:
        """Re-run abstraction on an existing document (fire-and-forget).
        Reconstructs projection text from stored chunks and dispatches a new
        semantic abstract as a background task; the abstract is written to
        the document node by that task, not by this call.

        Generation uses the SAGE stack's configured abstraction provider and
        model (``abstraction`` in ``sage/config.yaml``; stack-wide, not
        per-vault). If the new abstract is still off-topic, the lever is a
        stack-config change, not a re-issue of this tool.

        The call flips ``pipeline_status=abstraction_in_progress`` and
        returns ``{"status": "reabstract_started", "document_id",
        "dispatched_at"}``. The task ends at ``abstraction_complete`` or
        ``failed`` (with ``pipeline_error``). To observe the outcome, wait
        for a terminal ``pipeline_status`` -- a single caller-side wait that
        returns once the status leaves ``abstraction_in_progress``, not one
        status request per unit of caller work. A wait must also accept
        ``abstraction_interrupted``, which means the queue draining the work
        was stopped before it finished and the next server start re-runs it.
        A document left at ``abstraction_in_progress`` after a process
        restart never reaches a terminal status on its own: enumerate such
        documents via ``search(mode="catalog", filters={"pipeline_status":
        "abstraction_in_progress"})`` and re-issue ``recompute_abstract``.

        Error modes (synchronous; a background failure appears only as ``pipeline_status=failed``):
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``invalid_document_id`` (400)
        - ``document_not_found`` (404)
        - ``no_projection`` (404): no stored chunks to abstract from
        - ``reabstract_document_already_in_flight`` (409): ``detail.start_time`` names it
        - ``vault_migration_in_flight`` (409)
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            result = await v.ingestion_service.reabstract(document_id)
            return serialize(result)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    async def recompute_pipeline(
        vault_id: VaultIdParam,
        document_id: shaped_param(
            str,
            DocumentIdStr,
            "Document to re-run the pipeline against.",
        ),
    ) -> dict:
        """Re-run the full ingestion pipeline against an existing document.

        Operator repair for a document stuck at
        ``pipeline_status=projection_complete`` with no chunks, the state left
        when background indexing is lost. Projection re-runs from the
        document's ``source_path`` within this call, so a source or adapter
        failure is refused here. Indexing and abstraction then run as a
        background task, and the call returns without waiting for them.

        To observe the outcome, wait for a terminal ``pipeline_status`` with
        ``get_document``, as one bounded wait: ``abstraction_complete`` or
        ``abstraction_skipped`` on success, ``failed`` with ``pipeline_error``
        populated, or ``abstraction_interrupted``, which the next server start
        re-runs. A document left in ``indexing_in_progress`` or
        ``abstraction_in_progress`` after a process restart never reaches a
        terminal status on its own.

        One recompute runs per document at a time, while calls against
        different documents run in parallel. After a process-level kill,
        enumerate the stuck documents with a catalog ``search`` filtered on
        ``pipeline_status=projection_complete``, and re-issue this call
        against each.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``invalid_document_id`` (400)
        - ``document_not_found`` (404)
        - ``adapter_not_found``, ``adapter_config_invalid``, ``source_unreadable`` (400)
        - ``source_file_not_found`` (404): the retained source no longer resolves
        - ``recompute_pipeline_already_in_flight`` (409)
        - ``vault_migration_in_flight`` (409)
        - ``vault_source_store_refused`` (502) / ``vault_source_store_unavailable`` (503)
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            result = await v.ingestion_service.recompute_pipeline(document_id)
            return serialize(RecomputePipelineStartedResponse(**result))
        except (SAGEError, ValueError) as e:
            return error_response(e)

    # -------------------------------------------------------------------
    # SAGE maintenance API tools (CAS-ADR-029)
    #
    # Family-shared preconditions for every maintenance-family tool below:
    #
    # 1. ``vault_id`` is validated through the ``VaultIdStr`` typed alias
    #    (``_VAULT_ID_ADAPTER.validate_python``) before any vault lookup.
    #    Inputs that violate the typed-alias shape raise a structured
    #    ``ValueError`` rather than reaching the registry. See the CAS
    #    Typed-Alias Boundary Conventions for the shared validation
    #    contract.
    #
    # 2. The targeted vault must have been initialized with a
    #    ``registry_service``; otherwise ``v.maintenance_service`` is
    #    ``None`` and the tool raises ``RuntimeError``. This is primarily
    #    a test-fixture concern (production vault construction wires
    #    ``registry_service`` by default), but agents and integration
    #    tests that build vaults directly without the registry will hit
    #    this error rather than a silent no-op. The maintenance
    #    API surface is governed by CAS-ADR-029.
    #
    # These two preconditions apply to every maintenance-family tool below;
    # the per-tool docstrings surface only the caller-facing error codes
    # they produce (e.g. ``invalid_vault_id``, ``vault_not_found``).
    # -------------------------------------------------------------------

    @mcp.tool(name="migrate_vault", annotations=WRITE_ADDITIVE)
    async def migrate_vault(vault_id: VaultIdParam) -> dict:
        """Run the schema-migration surface's backfill and tier3-uniqueness scan.

        The durable store provisions its schema externally, so
        ``columns_added`` is always empty.

        ``backfills_applied`` names each data backfill that changed
        something. They clear the ``pipeline_error`` a recovered
        failure left behind, reduce a stored ``source_path`` in a spelling
        ingest no longer records to its plain form (each rewrite reported in
        ``source_paths_normalized``; a path that walks out of the source tree
        is left as recorded), bring stored passages and keyword vectors to
        the current shape, and drop a retired user table from the vault's
        schema.
        Only the heading backfill reads sources: a document whose source
        changed since it was indexed or cannot be read is listed once in
        ``documents_not_repaired`` and not read again until its source is
        restored. Nothing is re-abstracted, and only the documents a backfill
        rewrites are re-embedded.

        Idempotent: a re-call with nothing left to repair reports no error
        and empty lists, except ``tier3_uniqueness_activations``, which
        re-lists each clean declaration.

        **The passage-structure backfill is expensive, exclusive and
        one-time.** It rebuilds every passage index, vector index included:
        expect minutes of exclusive access at tens of thousands of passages.

        Run with no pipeline work in flight: this call is refused while any
        ingest, reabstract or recompute is queued or running on the vault,
        and they are refused while it runs. The exclusion covers this
        server process only.

        tier3 uniqueness: each ``unique_keys`` declaration in vault config
        whose data is clean gets a partial UNIQUE index, listed in
        ``tier3_uniqueness_activations``; one whose data violates it is
        recorded in ``tier3_uniqueness_collisions`` and not activated, and a
        previously clean index is preserved. **Inspect both fields** on
        every call.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``pipeline_work_in_flight`` (409): retry once it has drained
        - ``vault_migration_in_flight`` (409): another ``migrate_vault`` is running
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            if v.maintenance_service is None:
                raise RuntimeError(
                    f"Vault {vault_id!r} was initialized without a "
                    "registry_service; maintenance_service is unavailable."
                )
            report = await v.maintenance_service.migrate_vault()
            return serialize(report)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="verify_vault_drift", annotations=READ_ONLY)
    async def verify_vault_drift(vault_id: VaultIdParam) -> dict:
        """Audit active sync_target / derived_from edges for drift.

        Walks every active provenance-bearing edge in the vault and compares
        its recorded ``synced_from_*`` fields against the current head of the
        source's supersedes chain. Returns a DriftReport whose ``entries``
        enumerate edges needing operator attention; current edges are absent.
        Hash is the authoritative comparator; ``synced_from_version`` is a
        display key.

        ``StalenessBasis`` buckets — each ``DriftEntry`` carries a
        ``staleness_basis`` classifying why the edge surfaced:

        - ``content_drift``: the recorded ``synced_from_content_hash``
          differs from the current chain-head hash. Stale -- re-sync the
          dependent artifact.
        - ``chain_advanced_no_content_change``: the chain advanced past the
          recorded version but the head's content hash still matches.
          Informational.
        - ``recorded_null``: the edge predates the provenance columns.
          Informational -- back-filling is optional cleanup.
        - ``chain_nonlinear``: the source's supersedes chain forks (more than
          one head). Data-quality flag, not a drift signal; reconcile the
          chain first. ``current_head_*`` is null; ``competing_head_count``
          is populated.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``chain_nonlinear``: a report row, not an error, so one forked chain
          does not mask drift elsewhere
        - Graph-store query failures (500): infrastructure; retrying is appropriate
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            if v.maintenance_service is None:
                raise RuntimeError(
                    f"Vault {vault_id!r} was initialized without a "
                    "registry_service; maintenance_service is unavailable."
                )
            report = await v.maintenance_service.detect_drift()
            return serialize(report)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="verify_vault_source_files", annotations=READ_ONLY)
    async def verify_vault_source_files(
        vault_id: VaultIdParam,
        check_hashes: _INTEGRITY_CHECK_HASHES = False,
        document_ids: _INTEGRITY_DOCUMENT_IDS = None,
    ) -> dict:
        """Audit that every document's backing source file is present.

        Walks every document in the vault and checks that its
        ``source_path`` resolves to an existing file under the vault
        storage root. Returns a SourceFileIntegrityReport whose
        ``entries`` enumerate documents whose source file is missing;
        documents with an intact source file are absent. Read-only —
        mutates nothing.

        A recorded path that is a *link* rather than the retained copy
        surfaces as a ``symlinked`` entry, and one that resolves *outside*
        the vault's source tree as an ``out_of_root`` entry, which outranks
        ``missing``. Both are reported in both modes and never read through;
        the store refuses to write at either path, so repairing the document
        is refused until the link is removed or the path re-pointed.

        The ``hash_mismatch`` entries ``check_hashes`` adds are an integrity
        check on the stored copy, not a provenance check.

        This audits the vault-local source files (the ``imports/`` copies
        that ``get_document`` delivers), distinct from the content store
        that ``optimize_vault_content_store`` reclaims.

        A store refusal ends the walk rather than becoming a per-document
        status, so no report is returned; the audit is read-only and
        repeatable, so re-running it is the whole remedy for a transient one.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``invalid_document_id`` (400): an entry in ``document_ids``
        - ``document_scope_unmatched`` (404): ``detail.unmatched_ids`` lists every such id
        - ``vault_source_store_refused`` (502): resolve it at the store before retrying
        - ``vault_source_store_unavailable`` (503): the same call may succeed later
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            if document_ids is not None:
                document_ids = _DOCUMENT_ID_LIST_ADAPTER.validate_python(document_ids)
                # The request model carries the scope's remaining constraint --
                # at least one id -- so an empty scope is refused here by the
                # same rule, and naming the same parameter, as on the REST body.
                SourceFileIntegrityRequest(document_ids=document_ids)
            v = get_vault(vault_id)
            if v.maintenance_service is None:
                raise RuntimeError(
                    f"Vault {vault_id!r} was initialized without a "
                    "registry_service; maintenance_service is unavailable."
                )
            report = await v.maintenance_service.verify_vault_source_files(
                check_hashes=check_hashes, document_ids=document_ids
            )
            return serialize(report)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="restore_vault_source_file", annotations=WRITE_DESTRUCTIVE)
    async def restore_vault_source_file(
        vault_id: VaultIdParam,
        source: _RESTORE_SOURCE = None,
        document_id: _RESTORE_DOCUMENT_ID = None,
        transfer_token: _RESTORE_TRANSFER_TOKEN = None,
        sha256: _RESTORE_SHA256 = None,
    ) -> dict:
        """Repair a document's retained source file by writing delivered bytes back over it.

        The repair counterpart of ``verify_vault_source_files``. Re-ingesting
        cannot stand in: it would home the document at a second path and
        leave the damaged copy in place. This writes to the path the document
        record already names. The target is resolved from the delivered
        bytes' digest, so ``document_id`` is normally unnecessary.

        Writes nothing when the retained copy already hashes to its recorded
        digest, returning ``status: already_intact``. A recorded path that is
        a *link*, or that resolves *outside* the vault's source tree, is
        never reported that way: the write is refused
        (``vault_source_path_refused``). Remove the link or re-point the path,
        then re-run.

        ``status: restored`` does not by itself mean the record matches: it
        matches when ``stored_content_hash`` equals ``expected_content_hash``.
        ``record_refreshed`` false means the recorded digest was left alone:
        it follows the store only where the store demonstrably rewrote the
        bytes. ``provenance_verified`` false means nothing on the record could
        confirm the delivered bytes.

        Two-phase when the server cannot read the caller's filesystem: an
        absolute ``source`` returns an upload recipe (``status:
        upload_required``), the caller's environment delivers the bytes, and
        the call is repeated with the recipe's token as ``transfer_token``.

        Error modes:
        - 400: ``invalid_vault_id``, ``invalid_document_id``, ``invalid_sha256``
        - 400: ``ambiguous_ingest_source``, ``missing_ingest_source``
        - 400: ``restore_source_not_absolute``, ``restore_provenance_mismatch``
        - 400: ``source_digest_mismatch``, ``vault_source_path_refused``
        - ``vault_not_found`` (404)
        - 404: ``document_not_found``, ``source_file_not_found``, ``restore_target_unresolved``
        - 409: ``transfer_not_staged``
        - 410: ``transfer_token_invalid``
        - 500: ``transfer_endpoint_not_configured``
        - 502: ``vault_source_store_refused``
        - 503: ``vault_source_store_unavailable``
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if sha256 is not None:
                sha256 = _SHA256_ADAPTER.validate_python(sha256)
            v = get_vault(vault_id)
            if v.maintenance_service is None:
                raise RuntimeError(
                    f"Vault {vault_id!r} was initialized without a "
                    "registry_service; maintenance_service is unavailable."
                )
            # The service applies the caller-local delivery gate the ingest
            # path applies, so a caller generalizing that tool's completion
            # shape does not find this one narrower.
            result = await v.maintenance_service.restore_vault_source_file(
                source=source,
                document_id=document_id,
                transfer_token=transfer_token,
                sha256=sha256,
            )
            return serialize(result)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="recompute_deferred_vault_abstracts", annotations=WRITE_ADDITIVE)
    async def recompute_deferred_vault_abstracts(
        vault_id: VaultIdParam, include_pdf: _REABSTRACT_INCLUDE_PDF = False
    ) -> dict:
        """Backfill semantic abstracts for documents whose pipeline_status is abstraction_skipped.

        Enumerates documents in the named vault at
        ``pipeline_status=abstraction_skipped``, dispatches a reabstract per
        document, and polls until each reaches terminal status
        (``abstraction_complete``, ``abstraction_skipped``,
        ``abstraction_interrupted``, or ``failed``).

        Returns a ReabstractReport with per-document outcomes and aggregate
        counts. Each entry's outcome is one of ``success``, ``skipped_pdf``,
        ``llm_failure``, ``still_skipped``, ``timeout``, ``interrupted`` or
        ``dispatch_failed``. Every outcome beyond ``success`` and
        ``skipped_pdf`` counts toward ``failed_count``.

        The per-document poll is bounded: a document that has not settled
        within the server's wait ceiling is recorded with outcome ``timeout``,
        though its generation may still complete. It then sits at
        ``abstraction_in_progress``, which this operation does not enumerate.

        Reuses the in-process abstraction provider this MCP server loaded at
        startup; does NOT initialize a second provider. The standalone
        ``scripts/reabstract_deferred.py`` remains the operator fallback where
        no MCP server is running.

        Single-flight per vault: a concurrent call returns
        ``reabstract_already_in_flight`` (409), whose detail carries the
        running operation's ``start_time``, rather than queueing.

        Long-running: a pass takes roughly N times the per-document
        abstraction wall-clock, so allocate a generous client-side timeout;
        the tool returns a single ReabstractReport once the pass completes.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``reabstract_already_in_flight`` (409)
        - ``vault_migration_in_flight`` (409): retry once ``migrate_vault`` has returned
        - ``RuntimeError``: the vault is not wired for abstraction; a deployed vault always is
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            if v.maintenance_service is None:
                raise RuntimeError(
                    f"Vault {vault_id!r} was initialized without a "
                    "registry_service; maintenance_service is unavailable."
                )
            report = await v.maintenance_service.reabstract_deferred(include_pdf=include_pdf)
            return serialize(report)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="optimize_vault_content_store", annotations=WRITE_ADDITIVE)
    async def optimize_vault_content_store(
        vault_id: VaultIdParam, cleanup_older_than_days: _OPTIMIZE_CLEANUP_DAYS = 7
    ) -> dict:
        """Reclaim bloat in the per-vault content store.

        Wraps a ``VACUUM (FULL, ANALYZE)`` against every surface of the vault's
        content store: removes dead tuples, returns free space to the OS, and
        shrinks the relations. Postgres MVCC writes a new row version on every
        update or delete rather than reclaiming space in place, so disk usage
        on actively-churned vaults grows until this is called.

        Runs on its own autocommit connection (VACUUM cannot run inside a
        transaction block) and holds no lock that blocks concurrent reads or
        writes; a first run against a highly-churned vault
        may still take minutes and exceed an MCP client timeout, with
        subsequent runs settling to seconds.

        Returns an OptimizeContentStoreReport with pre/post observations
        (content-store byte size, retained version count, fragment counts),
        each taken over the same surfaces the reclamation ran against — the
        caller-visible evidence of reclamation, since the underlying
        operation itself returns nothing.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``ValueError``: ``cleanup_older_than_days`` is negative
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            if v.maintenance_service is None:
                raise RuntimeError(
                    f"Vault {vault_id!r} was initialized without a "
                    "registry_service; maintenance_service is unavailable."
                )
            report = await v.maintenance_service.optimize_content_store(
                cleanup_older_than_days=cleanup_older_than_days
            )
            return serialize(report)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    # -------------------------------------------------------------------
    # Server-level operational tools
    # -------------------------------------------------------------------

    @mcp.tool(name="reload_vault", annotations=WRITE_DESTRUCTIVE)
    async def reload_vault(vault_id: VaultIdParam) -> dict:
        """Reload a vault by closing its current services and reinitializing.

        When the vault was loaded from its ``vault_config.yaml`` declaration,
        the declaration is re-read through the vault-source store, so edits
        made outside SAGE take effect; a vault built from an in-memory
        configuration reuses it. Use this after editing the declaration, or
        when changes SAGE did not make -- another process, direct database
        writes -- have left the running services with stale data.

        Scope is per-vault, not stack-wide: only the target vault's
        declaration is re-read. The stack-wide configuration is captured at
        process start and changes only with a restart; read it with
        ``get_stack_config`` if you suspect drift.

        The reload builds the new services before tearing down the old ones.
        If construction fails, the error is returned and the vault keeps
        serving from its existing services, so the call can be retried once
        the cause is addressed. Abstraction work in flight does not survive a
        successful reload: a document being abstracted or waiting to be
        settles at ``abstraction_interrupted``, and nothing re-runs it until
        the next server start.

        ``document_count`` spans every lifecycle state, including archived
        predecessors -- it says how much a vault holds, not how much of it is
        current; ``get_vault_stats`` gives the per-state breakdown. It is
        null, never zero, when the count could not be read after the reload
        succeeded: the reload is still reported, and the null says the count
        is unknown rather than the vault empty.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``vault_config_validation_error`` (400): the declaration is not valid
          YAML or not a valid vault configuration; ``detail.errors`` names each
          problem
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            # ``get_vault`` raises ``VaultNotFoundError`` when ``vault_id`` is
            # not registered; the ``except`` block below routes it through
            # ``error_response`` as the ``vault_not_found`` envelope.
            get_vault(vault_id)
            # The registry service is resolved through its call-time getter;
            # see ``register_sage_tools``' docstring for the rationale.
            report = await get_vault_registry_service().reload_vault(vault_id)
        except (SAGEError, ValueError) as e:
            return error_response(e)
        return serialize(report)

    @mcp.tool(name="get_stack_config", annotations=READ_ONLY)
    async def get_stack_config() -> dict:
        """Return the SAGE-stack-wide configuration.

        Stack-wide configuration governs resources whose enforcement spans the
        whole SAGE process, such as the abstraction provider singleton;
        per-vault settings are read with ``get_vault_config``.

        The response carries the whole loaded configuration. ``profile`` is
        the active deployment-profile marker, such as ``local``: the
        stack-scope selection that co-binds the adapter ports. ``abstraction``
        carries ``provider``, the dispatch key (``local-mlx``, ``anthropic``,
        or ``stub``), and ``model``, the identifier passed to the provider's
        factory, which is null when the stack is stub-only.

        A field the stack leaves unset is reported with a null value rather
        than omitted, and new top-level sections may be added without changing
        the contract of existing callers. The configuration is captured at
        process start, so a change to it takes effect only after a restart;
        reloading a vault does not re-read it.
        """
        return get_stack_config_report()

    @mcp.tool(name="get_default_vault_config", annotations=READ_ONLY)
    async def get_default_vault_config(vault_id: _NEW_VAULT_ID) -> dict:
        """Return the default configuration a new vault would be created with.

        Returns the creation-time scaffold as a JSON object conforming to
        ``docs/fs/sage/vault_config.schema.json``: two doc types, the four
        base lifecycle states with their transition table, filename
        metadata extraction, tier-1 supersedes inference, and abstraction
        disabled.

        The scaffold precedes the vault. No vault with the supplied id
        need exist, none is created, and the response is not persisted.
        The id is required because it shapes ``storage_root`` and
        ``brain_root``, which the server derives rather than anything a
        caller can compute. ``vault.name`` and ``vault.owner`` come back
        empty for the caller to fill before passing the completed object
        to ``create_vault``.

        Error modes:
        - ``invalid_vault_id`` (400)
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            # The registry service is resolved through its call-time getter;
            # see ``register_sage_tools``' docstring for the rationale. No
            # vault is looked up: the scaffold precedes the vault it describes.
            return get_vault_registry_service().get_default_config(vault_id)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="verify_vault_retrieval", annotations=READ_ONLY)
    async def verify_vault_retrieval(vault_id: VaultIdParam) -> dict:
        """Run retrieval health assertions against the vault.

        Loads assertions from the YAML file referenced in
        ``retrieval_health.assertions_file`` of the vault config, runs
        each as a semantic search, and returns a pass/fail report.
        Each assertion is a ``(query, expected_document_id, top_k)``
        triple: the assertion passes when the expected document
        appears within the top-k results for the query. Failures
        report the actual rank (out of ``top_k * 5``) when the expected
        document was found beyond top-k, or ``null`` when it was not
        found at all. Used as a smoke test after bulk ingestion or
        configuration changes.

        The assertions YAML file is named by a path relative to the
        vault's ``storage_root`` and read through the vault-source store,
        addressed as a document's ``source_path`` is: under the filesystem
        binding (the local profile) it is read from ``storage_root`` on
        the server's disk; under the document-store binding (the cloud
        profile) it is read from the vault's folder in the document
        store, at the same relative path. A path leaving the storage
        root is refused. The file must have a top-level ``assertions:``
        key whose value is a list of objects; each object must include
        ``query`` and ``expected_document_id`` and may include ``top_k``
        (default 10).

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``assertions_not_configured`` (400): no ``retrieval_health.assertions_file`` entry
        - ``assertions_file_invalid`` (400): malformed, wrongly structured, or
          outside ``storage_root``
        - ``assertions_file_not_found`` (404)
        - ``vault_source_store_refused`` (502): ``detail.store_status`` carries the store's status
        - ``vault_source_store_unavailable`` (503): the same call may succeed later
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            report = await v.utilities_service.eval_retrieval()
            return serialize(report)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="export_projection", annotations=WRITE_DESTRUCTIVE)
    async def export_projection(
        vault_id: VaultIdParam,
        document_id: _EXPORT_DOCUMENT_ID,
        output_path: _EXPORT_OUTPUT_PATH,
    ) -> dict:
        """Write stored projection to a Markdown file for inspection.

        Exports the stored projection text for a document to a
        specified file path inside the vault's ``storage_root``. Used for
        debugging and manual inspection of the adapter's output. The
        projection is the structured plain-text rendering produced by
        the source adapter during ingestion.

        ``output_path`` may be relative (resolved against ``storage_root``)
        or absolute, but it must resolve to a file location inside the
        vault's ``storage_root``. Targets outside the vault tree, and the
        root itself, are refused with ``path_traversal_denied``; a target
        that is not a file location -- an existing directory, or a path
        through an existing file -- is refused with ``output_path_invalid``,
        whose ``detail.reason`` says which. Both refusals come before any read,
        and nothing is written. Missing parent directories are created,
        and a file already at the target is overwritten.

        The destination is a path in the server's own vault tree, which a
        caller can read back only when it shares that filesystem. Under the
        cloud profile the export is refused with
        ``caller_filesystem_unavailable`` before any read; ``read_projection``
        delivers the projection to the caller instead.

        Error modes:
        - ``invalid_vault_id``, ``invalid_document_id`` (400)
        - ``path_traversal_denied``, ``output_path_invalid`` (400): see above
        - ``vault_not_found`` (404)
        - ``document_not_found`` (404)
        - ``no_projection`` (404): e.g. ingestion failed mid-pipeline
        - ``caller_filesystem_unavailable`` (501): cloud profile
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            response = await v.utilities_service.export_projection(document_id, output_path)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    return {
        "ingest_document": ingest_document,
        "get_filename_metadata": get_filename_metadata,
        "recompute_abstract": recompute_abstract,
        "recompute_pipeline": recompute_pipeline,
        "get_document": get_document,
        "update_metadata": update_metadata,
        "update_lifecycles": update_lifecycles,
        "create_edges": create_edges,
        "delete_edge": delete_edge,
        "verify_preconditions": verify_preconditions,
        "traverse": traverse,
        "chain": chain,
        "search": search,
        "read_projection": read_projection,
        "read_section": read_section,
        "list_headings": list_headings,
        "recompute_views": recompute_views,
        "list_vaults": list_vaults,
        "create_vault": create_vault,
        "get_vault_config": get_vault_config,
        "update_vault_config": update_vault_config,
        "get_vault_stats": get_vault_stats,
        "verify_hashes": verify_hash,
        "list_staging_edges": list_staging_edges,
        "update_staging_edge": update_staging_edge,
        "list_pending_metadata": list_pending_metadata,
        "migrate_vault": migrate_vault,
        "verify_vault_drift": verify_vault_drift,
        "verify_vault_source_files": verify_vault_source_files,
        "restore_vault_source_file": restore_vault_source_file,
        "recompute_deferred_vault_abstracts": recompute_deferred_vault_abstracts,
        "optimize_vault_content_store": optimize_vault_content_store,
        "reload_vault": reload_vault,
        "get_stack_config": get_stack_config,
        "get_default_vault_config": get_default_vault_config,
        "verify_vault_retrieval": verify_vault_retrieval,
        "export_projection": export_projection,
    }
