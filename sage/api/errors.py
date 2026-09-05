"""SAGE error hierarchy and FastAPI exception handlers.

Exception classes carry structured detail dicts matching the OpenAPI
ErrorResponse schema. The exception handler converts them to JSON responses.
"""

from datetime import datetime
from enum import StrEnum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from sage.config import render_state_set
from sage.models.enums import EdgeType, SourceType
from sage.models.schemas import ErrorResponse


class SAGEError(Exception):
    """Base exception for SAGE API errors."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail


class DocumentNotFoundError(SAGEError):
    """404: no document resolves to the supplied id.

    The error ``code`` is always ``document_not_found``; the ``detail``
    dict differentiates the root cause (CAS-ADR-039). Read-path callers
    pass a pre-built ``detail`` carrying the discriminators
    ``id_well_formed``, ``ever_existed``, and ``slug_matches_catalog`` so
    a caller can tell a malformed id from a never-existed id from a
    real-but-renamed one without a second probing round-trip. Write-path
    and graph callers omit ``detail`` and get the bare
    ``{"document_id": ...}`` form.
    """

    def __init__(self, document_id: str, detail: dict | None = None) -> None:
        super().__init__(
            "document_not_found",
            f"Document {document_id} not found",
            404,
            detail if detail is not None else {"document_id": document_id},
        )


class InvalidDocumentIdError(SAGEError):
    """400: the supplied document_id is not a well-formed id.

    A syntactically-malformed id is rejected at the request boundary and
    surfaces as this structured 400 — distinct from the 404
    ``document_not_found`` discriminator, which fires only for a
    well-formed id that resolves to no document. The boundary precedes
    the discriminator by design: malformed *syntax* is a client error,
    not a miss. The ``detail`` carries the offending value so a caller
    can correct its call without parsing the message.
    """

    def __init__(self, document_id: str) -> None:
        super().__init__(
            "invalid_document_id",
            f"document_id {document_id!r} is not a well-formed document id "
            "(expected 8 hex characters, an underscore, then a lowercase "
            "alphanumeric/underscore slug)",
            400,
            {"document_id": document_id},
        )


#: Single source of truth for the typed-alias boundary-error family. Shared by
#: ``translate_validation_error`` (which rebuilds the structured 400 from the
#: validator's ctx) and the MCP ``_error_response`` choke point (which envelopes
#: these codes instead of the generic ``internal_error``). ``invalid_document_id``
#: keeps its own ``InvalidDocumentIdError`` but joins the set so both dispatch
#: points recognize the whole family from one place. Adding a future alias means
#: adding its code here plus raising the shared-shape ``PydanticCustomError`` in
#: its validator -- no new error class or dispatch branch.
_TYPED_ALIAS_CODES: frozenset[str] = frozenset(
    {
        "invalid_document_id",
        "invalid_vault_id",
        "invalid_edge_id",
        "invalid_sha256",
        "invalid_function_id",
        "invalid_document_date",
        "invalid_user_id",
    }
)


class InvalidTypedAliasError(SAGEError):
    """400: a typed-alias boundary value failed its shape validator.

    One parameterized error for the typed-alias family (vault_id, edge_id,
    sha256, function_id, document_date, user_id). The leaf-layer validator in
    ``sage/models/schemas.py`` raises a ``PydanticCustomError`` carrying a
    uniform ``{argument, value, expected}`` ctx; the request-boundary translator
    rebuilds this structured 400 from that ctx, so a malformed value surfaces as
    a caller-actionable ``invalid_<argument>`` code -- with the offending value
    and the expected shape in ``detail`` -- instead of the generic
    ``internal_error`` (MCP) / native 422 (HTTP). ``invalid_document_id`` keeps
    its own ``InvalidDocumentIdError``.
    """

    def __init__(self, code: str, argument: str, value: object, expected: str) -> None:
        super().__init__(
            code,
            f"{argument} {value!r} is not a well-formed {argument} (expected {expected})",
            400,
            {argument: value, "expected": expected},
        )


class InvalidLifecycleTransitionError(SAGEError):
    """409: action is known but invalid from current state (BH-012)."""

    def __init__(
        self,
        current_state: str,
        attempted_action: str,
        valid_actions: list[str],
        pipeline_status: str | None = None,
    ) -> None:
        detail: dict = {
            "current_state": current_state,
            "attempted_action": attempted_action,
            "valid_actions": valid_actions,
        }
        if pipeline_status is not None:
            detail["pipeline_status"] = pipeline_status
        super().__init__(
            "invalid_lifecycle_transition",
            f"Cannot {attempted_action} from {current_state}",
            409,
            detail,
        )


class InvalidActionError(SAGEError):
    """400: action value is not in any transition table."""

    def __init__(self, action: str) -> None:
        super().__init__(
            "invalid_action",
            f"Unknown action: {action}",
            400,
        )


class DuplicateContentError(SAGEError):
    """409: same source_path + hash already exists (BH-018)."""

    def __init__(self, existing_document_id: str, source_content_hash: str) -> None:
        super().__init__(
            "duplicate_content",
            "Duplicate content detected",
            409,
            {
                "existing_document_id": existing_document_id,
                "source_content_hash": source_content_hash,
            },
        )


class ForceReingestPathMismatchError(SAGEError):
    """409: a force-reingest resolved a content-hash match to a record stored
    at a different source_path than the incoming file, and the caller did not
    confirm the target.

    Force-reingest keys its target by content hash alone. When two files are
    byte-identical but live at different paths, the hash match may be an
    unrelated document rather than the one the caller meant to re-ingest.
    Overwriting it would silently discard that document's identity, so the
    substrate refuses until the caller confirms the intended record via
    ``document_id``. Same-path force-reingest (BH-019) never trips this.
    """

    def __init__(
        self,
        resolved_id: str,
        resolved_source_path: str,
        incoming_source_path: str,
        content_hash: str,
    ) -> None:
        super().__init__(
            "force_reingest_path_mismatch",
            (
                "Force re-ingest matched an existing document at a different "
                "source_path by content hash alone; pass document_id to "
                "confirm the record to overwrite."
            ),
            409,
            {
                "existing_document_id": resolved_id,
                "existing_source_path": resolved_source_path,
                "new_source_path": incoming_source_path,
                "source_content_hash": content_hash,
            },
        )


class MissingFieldError(SAGEError):
    """400: required field missing from request."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(
            f"missing_{field}",
            message,
            400,
        )


class InvalidDocTypeError(SAGEError):
    """400: doc_type not in vault's document_types config."""

    def __init__(self, doc_type: str, valid_types: set[str]) -> None:
        super().__init__(
            "invalid_doc_type",
            f"Unknown doc_type: {doc_type}",
            400,
            {"doc_type": doc_type, "valid_types": sorted(valid_types)},
        )


class Tier3SchemaViolationError(SAGEError):
    """400: tier3_metadata payload failed validation.

    Two failure modes share this error code:

      (1) The resolved doc_type has no metadata_schema declared in vault
          config (strict no-loose-mode). ``path`` is empty; ``message``
          says so explicitly.
      (2) The payload was validated against the declared metadata_schema
          and failed. ``path`` is the JSON Pointer to the offending field
          (from ``jsonschema.ValidationError.json_path``); ``message`` is
          the validator's own error message.
    """

    def __init__(
        self,
        doc_type: str,
        path: str,
        message: str,
        instance: object | None = None,
    ) -> None:
        detail: dict = {
            "doc_type": doc_type,
            "path": path,
            "message": message,
        }
        if instance is not None:
            detail["instance"] = instance
        super().__init__(
            "tier3_schema_violation",
            f"tier3_metadata violates schema for doc_type '{doc_type}': {message}",
            400,
            detail,
        )


class Tier3DocTypeChangeStaleKeysError(SAGEError):
    """400: doc_type is being changed in the same call as a tier3_metadata
    ops object, and the merged tier3 dict carries keys that are not in the
    new doc_type's metadata_schema properties.

    The post-merge `_validate_tier3` call would catch this as a generic
    `tier3_schema_violation` (additionalProperties: false fires on the first
    stale key), but the caller cannot tell from that error whether their
    patch is wrong for the new schema or whether they merely forgot to
    `unset` the legacy keys. This error is raised before validation runs
    and names the exact list of keys the caller must add to `unset` to
    satisfy the new schema. When the new doc_type has no metadata_schema,
    every merged key is stale by definition.
    """

    def __init__(
        self,
        document_id: str,
        previous_doc_type: str,
        new_doc_type: str,
        stale_keys: list[str],
        merged_tier3_keys: list[str],
    ) -> None:
        super().__init__(
            "tier3_doc_type_change_stale_keys",
            (
                f"Cannot change doc_type from {previous_doc_type!r} to "
                f"{new_doc_type!r} without also unsetting stale tier3_metadata "
                f"keys: {sorted(stale_keys)!r}"
            ),
            400,
            {
                "document_id": document_id,
                "previous_doc_type": previous_doc_type,
                "new_doc_type": new_doc_type,
                "stale_keys": sorted(stale_keys),
                "merged_tier3_keys": sorted(merged_tier3_keys),
            },
        )


class StaleReadError(SAGEError):
    """409: caller's `expected_version` does not match the document's
    current version at write time (CAS-ADR-038 Primitive B).

    Raised when a scalar-metadata write supplies an `expected_version`
    that does not equal the document's current `updated_at`. The detail
    envelope carries the current version so the caller can refetch
    and retry without an extra round-trip. Mirror of
    `Tier3UniqueConstraintViolation` shape per CAS-ADR-031.
    """

    def __init__(
        self,
        document_id: str,
        expected_version: str,
        current_version: str,
    ) -> None:
        super().__init__(
            "stale_read",
            (
                f"Document {document_id!r} version mismatch: "
                f"expected {expected_version!r}, current {current_version!r}"
            ),
            409,
            {
                "document_id": document_id,
                "expected_version": expected_version,
                "current_version": current_version,
            },
        )


class StaleChainHeadError(SAGEError):
    """409: caller's `expected_head_version` does not match the chain
    head's current version at supersede time (CAS-ADR-038 Primitive C).

    Raised when an ingest with `predecessor_id` supplies an
    `expected_head_version` that does not equal the predecessor's
    current `updated_at` at the moment the supersede is about to run.
    The detail envelope carries the current head id and version so the
    caller can pivot through the chain (use `current_head_id` as the
    next supersede's `predecessor_id`) and retry without an extra
    round-trip. Preserves the linear-chain invariant in CAS-ADR-023 by
    surfacing the conflict instead of letting two concurrent supersedes
    each create their own version, forking the chain into a tree.
    """

    def __init__(
        self,
        predecessor_id: str,
        expected_head_version: str,
        current_head_id: str,
        current_head_version: str,
    ) -> None:
        super().__init__(
            "stale_chain_head",
            (
                f"Chain head version mismatch for predecessor {predecessor_id!r}: "
                f"expected {expected_head_version!r}, current head "
                f"{current_head_id!r} at {current_head_version!r}"
            ),
            409,
            {
                "predecessor_id": predecessor_id,
                "expected_head_version": expected_head_version,
                "current_head_id": current_head_id,
                "current_head_version": current_head_version,
            },
        )


class ExpectedHeadVersionRequiresPredecessorError(SAGEError):
    """400: `expected_head_version` was supplied without a `predecessor_id`.

    The two parameters anchor each other: `expected_head_version` is a
    compare-and-swap token bound to the chain head identified by
    `predecessor_id`. Without a predecessor there is no chain head to
    compare against, so the parameter has no defined meaning. Surface
    the conflict explicitly rather than silently ignoring the token.
    """

    def __init__(self) -> None:
        super().__init__(
            "expected_head_version_requires_predecessor",
            (
                "`expected_head_version` requires `predecessor_id`; "
                "the version token is bound to the chain head identified "
                "by the predecessor."
            ),
            400,
            None,
        )


class Tier3UniqueConstraintViolation(SAGEError):
    """409: tier3_metadata field value collides with the declared uniqueness
    constraint (CAS-ADR-031).

    Raised by the storage substrate when an insert or supersession-insert
    would violate a `unique_keys` declaration on the resolved doc_type.
    The supersession chain is the explicit exception: a successor inherits
    its predecessor's identifier without collision because the substrate
    marks the predecessor superseded before inserting the successor, and
    the partial UNIQUE index excludes superseded rows.

    Callers detect this error to drive a retry path (e.g., the
    cas-ticket-management skill's W1.1 allocator re-runs its existence
    check and fallback scan on this 409, then propagates the error if a
    single retry does not converge).
    """

    def __init__(
        self,
        doc_type: str,
        field: str,
        colliding_value: object,
        existing_document_id: str,
    ) -> None:
        super().__init__(
            "tier3_unique_constraint_violation",
            (
                f"tier3_metadata field {field!r} value {colliding_value!r} "
                f"is already held by document {existing_document_id!r} "
                f"in doc_type {doc_type!r}"
            ),
            409,
            {
                "doc_type": doc_type,
                "field": field,
                "colliding_value": colliding_value,
                "existing_document_id": existing_document_id,
            },
        )


class ListFieldAddConflictError(SAGEError):
    """400: a ListFieldPatch.add carries one or more values already present
    on the named list-valued field (CAS-ADR-038 Primitive A).

    The error code derives from the field name: ``{field}_add_conflict``.
    The detail envelope carries ``document_id``, the conflicting subset
    keyed by the field name, and the stored list keyed
    ``current_{field}``.
    """

    def __init__(
        self,
        field: str,
        document_id: str,
        values: list[str],
        current: list[str],
    ) -> None:
        super().__init__(
            f"{field}_add_conflict",
            (f"Cannot add {field} already present on document {document_id}: {sorted(values)!r}"),
            400,
            {
                "document_id": document_id,
                field: sorted(values),
                f"current_{field}": current,
            },
        )


class ListFieldRemoveConflictError(SAGEError):
    """400: a ListFieldPatch.remove carries one or more values absent from
    the named list-valued field (CAS-ADR-038 Primitive A).

    Code: ``{field}_remove_conflict``. Detail envelope mirrors
    ``ListFieldAddConflictError``.
    """

    def __init__(
        self,
        field: str,
        document_id: str,
        values: list[str],
        current: list[str],
    ) -> None:
        super().__init__(
            f"{field}_remove_conflict",
            (f"Cannot remove {field} absent from document {document_id}: {sorted(values)!r}"),
            400,
            {
                "document_id": document_id,
                field: sorted(values),
                f"current_{field}": current,
            },
        )


class TagPatchOverlapError(SAGEError):
    """400: ListFieldPatch.add and remove lists share entries, or one list contains duplicates."""

    def __init__(self, violation: str, tags: list[str]) -> None:
        super().__init__(
            "tag_patch_overlap",
            f"ListFieldPatch invalid: {violation}",
            400,
            {"violation": violation, "tags": sorted(tags)},
        )


class Tier3UnsetConflictError(SAGEError):
    """400: Tier3Patch.unset includes one or more keys absent from the stored tier3_metadata."""

    def __init__(
        self,
        document_id: str,
        doc_type: str | None,
        keys: list[str],
        current_tier3_keys: list[str],
    ) -> None:
        super().__init__(
            "tier3_unset_conflict",
            (
                f"Cannot unset tier3_metadata keys absent on document "
                f"{document_id}: {sorted(keys)!r}"
            ),
            400,
            {
                "document_id": document_id,
                "doc_type": doc_type,
                "keys": sorted(keys),
                "current_tier3_keys": sorted(current_tier3_keys),
            },
        )


class Tier3PatchOverlapError(SAGEError):
    """400: Tier3Patch.set and unset share keys."""

    def __init__(self, keys: list[str]) -> None:
        super().__init__(
            "tier3_patch_overlap",
            f"Tier3Patch set and unset must be disjoint; overlap: {sorted(keys)!r}",
            400,
            {"keys": sorted(keys)},
        )


class PatchEmptyError(SAGEError):
    """400: a patch object was supplied but carries no actionable operation.

    Examples: ``tags={}``, ``tags={"add": []}``, ``tier3_metadata={"set": {}}``.
    Caught at Pydantic request validation; the empty-shape forms are almost
    always serialization-stripped bugs rather than intentional no-ops.
    """

    def __init__(self, field: str) -> None:
        super().__init__(
            "patch_empty",
            (
                f"{field} patch carries no actionable operation; supply a non-empty "
                "operation key or omit the field entirely"
            ),
            400,
            {"field": field},
        )


class LegacyFormError(SAGEError):
    """400: caller passed the deprecated bare-list / bare-dict shape on a patch field.

    Surfaced at the MCP boundary so callers familiar with the pre-patch
    contract receive a structured error naming the new ops-object shape
    rather than a generic Pydantic validation error.
    """

    def __init__(self, field: str, received_type: str, example: str) -> None:
        super().__init__(
            "legacy_form",
            (f"{field} no longer accepts the {received_type} form. Use the ops object: {example}"),
            400,
            {"field": field, "received_type": received_type, "example": example},
        )


class MisplacedMetadataError(SAGEError):
    """400: caller spelled nested metadata fields as top-level ingest arguments.

    ``ingest_document`` takes caller metadata nested under ``metadata``.
    The recognized keys are also published as top-level parameters, but
    only as tripwires: an unpublished parameter is stripped by MCP
    clients that coerce arguments to the published schema, so a
    misplaced field would be discarded before the server could object.
    Publishing the spellings makes the mistake reachable; this error
    makes it loud.

    Sibling of ``LegacyFormError``: both turn a wrong-shape call that
    would otherwise be silently partially applied into a structured
    rejection naming the accepted shape.
    """

    def __init__(self, fields: list[str], recognized: list[str], example: str) -> None:
        joined = ", ".join(fields)
        super().__init__(
            "misplaced_metadata",
            (
                f"{joined} must be nested under `metadata`, not passed as a "
                f"top-level argument. Use: {example}"
            ),
            400,
            {"fields": fields, "recognized": recognized, "example": example},
        )


class MisplacedFilterError(SAGEError):
    """400: caller spelled nested filter keys as top-level search arguments.

    ``search`` takes its scope constraints nested under ``filters``. The
    recognized keys are also published as top-level parameters, but only
    as tripwires: an unpublished parameter is stripped by MCP clients
    that coerce arguments to the published schema, so a misplaced key
    would be discarded before the server could object. Publishing the
    spellings makes the mistake reachable; this error makes it loud.

    Read-side sibling of ``MisplacedMetadataError``. The write-side case
    costs a mis-titled document, which is visible; this one costs an
    unfiltered result set, which is not -- the caller receives plausible
    rows carrying no indication that the constraint was dropped.
    """

    def __init__(self, fields: list[str], recognized: list[str], example: str) -> None:
        joined = ", ".join(fields)
        super().__init__(
            "misplaced_filters",
            (
                f"{joined} must be nested under `filters`, not passed as a "
                f"top-level argument. Use: {example}"
            ),
            400,
            {"fields": fields, "recognized": recognized, "example": example},
        )


class InvalidModeError(SAGEError):
    """400: discover mode value is not in the RetrievalMode enum."""

    def __init__(self, mode: str, valid_modes: list[str]) -> None:
        super().__init__(
            "invalid_mode",
            f"Unknown discover mode: {mode!r}. Valid modes: {sorted(valid_modes)!r}",
            400,
            {"mode": mode, "valid_modes": sorted(valid_modes)},
        )


class InvalidFilterValueError(SAGEError):
    """400: a filter value falls outside a closed vocabulary.

    Applies to filter fields typed against a Python enum, where the
    accepted set is fixed in code rather than per-vault configuration.
    Such a value can never match a stored row, so refusing it is more
    useful than returning an empty result the caller cannot distinguish
    from a genuine zero-match. The valid set travels with the error so
    the caller can self-correct without a probe round-trip.

    Vault-configured vocabularies (doc_type, lifecycle_status) cannot be
    checked here -- the accepted set is not known until the request is
    resolved against a vault -- and surface as a non-fatal hint on the
    response instead.
    """

    def __init__(self, field: str, value: object, valid_values: list[str]) -> None:
        super().__init__(
            "invalid_filter_value",
            (
                f"Invalid value {value!r} for filter {field!r}. "
                f"Valid values: {sorted(valid_values)!r}."
            ),
            400,
            {"field": field, "value": value, "valid_values": sorted(valid_values)},
        )


class StorageQueryFailedError(SAGEError):
    """500: the storage backend refused a filtered document query.

    The driver's own rejection quotes the failing statement and any
    backend hint. Returned verbatim that becomes an unstructured leak of
    internal query shape through what is otherwise a typed envelope, and
    a caller cannot act on it either way. This carries the operation that
    failed and nothing more; the driver text is logged where an operator
    can read it.

    A caller reaching this has found a defect rather than a usable
    correction, which is why it is a 500 and carries no remediation
    hint -- unlike the 400-class filter errors above, where the request
    itself is the thing to fix.
    """

    def __init__(self, operation: str) -> None:
        super().__init__(
            "storage_query_failed",
            (
                "The storage backend refused the query. This is a defect, not "
                "a correctable request; the failure has been logged for the "
                "vault operator."
            ),
            500,
            {"operation": operation},
        )


class UnknownFilterKeyError(SAGEError):
    """400: a key in `filters` is not in RetrievalFilters.

    Pre-, unknown filter keys were silently dropped by Pydantic's
    default extra="ignore" behavior — a typo footgun where a misspelled
    `tickett_id` matched every row. ``extra="forbid"`` on RetrievalFilters
    now surfaces these typos as a typed error.
    """

    def __init__(self, key: str, valid_keys: list[str], example: str) -> None:
        super().__init__(
            "unknown_filter_key",
            (f"Unknown filter key {key!r}. Valid keys: {sorted(valid_keys)!r}. Example: {example}"),
            400,
            {"key": key, "valid_keys": sorted(valid_keys), "example": example},
        )


class InvalidFilterShapeError(SAGEError):
    """400: a value in `filters` has the wrong type for its field.

    e.g. ``filters={"tags": 42}`` passed an int where ``list[str] | None``
    was expected.
    """

    def __init__(self, field: str, expected_type: str, received_type: str) -> None:
        super().__init__(
            "invalid_filter_shape",
            (
                f"filters[{field!r}] has wrong type: expected {expected_type}, "
                f"received {received_type}"
            ),
            400,
            {
                "field": field,
                "expected_type": expected_type,
                "received_type": received_type,
            },
        )


class ModeParameterMismatchError(SAGEError):
    """400: a parameter is set that is forbidden by the chosen discover mode.

    Distinct from `missing_*` codes which fire on the inverse case
    (parameter required for the chosen mode but absent — e.g.,
    `missing_query` for semantic mode). This error fires when the parameter
    IS present but is not valid for the chosen mode (e.g., catalog mode
    with `heading_path`, which is deterministic-only).
    """

    def __init__(
        self,
        mode: str,
        forbidden_param: str,
        allowed_modes: list[str],
    ) -> None:
        super().__init__(
            "mode_parameter_mismatch",
            (
                f"Parameter {forbidden_param!r} is not valid for mode {mode!r}. "
                f"Allowed modes for {forbidden_param!r}: {sorted(allowed_modes)!r}"
            ),
            400,
            {
                "mode": mode,
                "forbidden_param": forbidden_param,
                "allowed_modes": sorted(allowed_modes),
            },
        )


class InvalidParameterError(SAGEError):
    """422: a request parameter failed validation with no more specific code.

    The general case behind the specific ones. `invalid_filter_value`,
    `invalid_filter_shape`, `invalid_mode` and the typed-alias family each
    report something this cannot -- an accepted value set, an expected type,
    an enum's members -- and are preferred wherever they apply. This code
    covers what is left: bound violations and type-coercion failures on
    ordinary request parameters, which would otherwise reach the caller with
    no envelope at all.

    Built from the structured fields of the underlying validation error
    rather than from its rendered text, so the model class name and the
    validator's documentation URL -- both present only in the rendering --
    cannot reach a caller. See `validation_error_envelope`.

    The 422 status matches what request validation already returns on the
    HTTP surface, so adopting this envelope changes the response body
    without moving any endpoint's status code.
    """

    def __init__(
        self,
        parameter: str,
        value: object,
        constraint: str,
        hint: str | None = None,
    ) -> None:
        message = f"Invalid value for parameter {parameter!r}: {constraint}."
        detail: dict = {
            "parameter": parameter,
            "value": value,
            "constraint": constraint,
        }
        if hint is not None:
            message = f"{message} {hint}"
            detail["hint"] = hint
        super().__init__("invalid_parameter", message, 422, detail)


class PipelineIncompleteError(SAGEError):
    """422: document has incomplete/failed pipeline."""

    def __init__(self, document_id: str) -> None:
        super().__init__(
            "pipeline_incomplete",
            f"Document {document_id} has incomplete pipeline",
            422,
            {"document_id": document_id},
        )


class HeadingNotFoundError(SAGEError):
    """404: heading path not found in document (BH-030).

    ``available_headings`` is the full enumeration. ``candidate_matches``
    is a substring-match shortlist (case-insensitive) computed by the
    caller — useful when the query is the *tail* of a stored path (e.g.
    "CLAIMS" against a stored "CLAIMS -- Remove Before Filing") so the
    caller can retry with the exact path in one extra round-trip.
    """

    def __init__(
        self,
        heading_path: str,
        document_id: str,
        available_headings: list[str] | None = None,
        candidate_matches: list[str] | None = None,
    ) -> None:
        detail: dict = {"heading_path": heading_path, "document_id": document_id}
        if candidate_matches:
            detail["candidate_matches"] = candidate_matches
        if available_headings is not None:
            detail["available_headings"] = available_headings
        super().__init__(
            "heading_not_found",
            f"Heading '{heading_path}' not found in document {document_id}",
            404,
            detail,
        )


class SelfReferentialEdgeError(SAGEError):
    """400: source_id and target_id are the same document."""

    def __init__(self, document_id: str) -> None:
        super().__init__(
            "self_referential_edge",
            f"Cannot create edge from document {document_id} to itself",
            400,
            {"document_id": document_id},
        )


class AdapterNotFoundError(SAGEError):
    """400: no adapter registered for source type."""

    def __init__(self, adapter: str) -> None:
        super().__init__(
            "adapter_not_found",
            f"No adapter registered for source type: {adapter}",
            400,
        )


class SourceFileNotFoundError(SAGEError):
    """404: source file does not exist at the resolved path."""

    def __init__(self, source: str) -> None:
        super().__init__(
            "source_file_not_found",
            f"Source file not found: {source}",
            404,
            {"source": source},
        )


class RestoreTargetUnresolvedError(SAGEError):
    """404: no single document claims the delivered bytes as its provenance.

    A restore names its target by content: the bytes handed over must be the
    ones some document was ingested from. Nothing matched, or more than one
    document did, so there is no unambiguous copy to write over -- and writing
    to a guessed path would corrupt a document that was intact. The caller
    resolves it by supplying ``document_id``.
    """

    def __init__(self, content_hash: str, candidate_ids: list[str]) -> None:
        if candidate_ids:
            message = (
                f"{len(candidate_ids)} documents share the delivered bytes' digest "
                f"{content_hash}; supply document_id to name the one to restore."
            )
        else:
            message = (
                f"No document was ingested from the delivered bytes (digest "
                f"{content_hash}). Deliver the original source bytes, or supply "
                f"document_id to name the target explicitly."
            )
        super().__init__(
            "restore_target_unresolved",
            message,
            404,
            {"content_hash": content_hash, "candidate_ids": candidate_ids},
        )


class RestoreProvenanceMismatchError(SAGEError):
    """400: the pinned document was not ingested from the delivered bytes.

    ``document_id`` names which copy to write over; it does not license writing
    *arbitrary* bytes there. Without this check a pin turns the repair into its
    opposite: the delivered file overwrites the retained copy and the record is
    refreshed to describe it, so the integrity audit goes green over a document
    whose stored bytes are now something else entirely -- erasing the very
    evidence the audit exists to preserve.

    Not raised for a record whose ``stored_content_hash`` is null. Such a record
    predates the delivered/stored digest split and its provenance hash describes
    the stored copy rather than the delivered bytes, so a caller re-delivering
    the original cannot match it -- which is the case the pin exists to serve.
    The refresh rule in ``restore_vault_source_file`` is what keeps that
    exemption from laundering: a store that returns what it was handed licenses
    no digest update.
    """

    def __init__(self, document_id: str, delivered_hash: str, recorded_hash: str) -> None:
        super().__init__(
            "restore_provenance_mismatch",
            (
                f"Document {document_id} was not ingested from the delivered bytes "
                f"(delivered {delivered_hash}, recorded {recorded_hash}). Deliver "
                f"that document's original source bytes, or drop document_id to "
                f"resolve the target from the bytes themselves."
            ),
            400,
            {
                "document_id": document_id,
                "delivered_content_hash": delivered_hash,
                "recorded_content_hash": recorded_hash,
            },
        )


class VaultSourcePathRefusedError(SAGEError):
    """400: the vault-source store refused to write at the path it was given.

    Carries the binding's own reason rather than restating one. A binding
    refuses a write target for several distinct causes -- the path is absolute,
    it walks out of the vault's source tree, a symlink or a directory sits at
    the destination, it resolves outside the source root through an ancestor,
    or something that is not a directory sits where its parent belongs -- and
    a fixed message can only describe one of them, leaving the others reported
    as something that did not happen. The reason names the destination by its
    vault-relative spelling, never by a server-side absolute path.

    Exists because the binding raises a plain ``ValueError`` subclass: it sits
    below the API layer and may not import it, so without a translation at the
    service boundary the refusal reaches an MCP caller as a generic internal
    error and an HTTP caller as a bare 500 against a spec that declares neither.
    """

    def __init__(self, source_path: str, reason: str) -> None:
        super().__init__(
            "vault_source_path_refused",
            reason,
            400,
            {"source_path": source_path},
        )


class VaultSourceStoreRefusedError(SAGEError):
    """502: the vault-source store refused the operation on its merits.

    The store was reachable and answered; it declined. A quota it will not
    exceed, a permission it no longer grants, a reply that accepted an upload
    session and named no URL to write to, a session it committed at the wrong
    fragment. Repeating the request reproduces the answer, so the operator has
    to act on the store before the operation can succeed --
    :class:`VaultSourceStoreUnavailableError` is the counterpart for the
    refusals where waiting is the whole remedy.

    502 rather than a 4xx: the request that reached SAGE was well formed, and
    the fault is an upstream one SAGE is reporting rather than committing.

    The message is composed here rather than forwarded from the binding. The
    store's own response body names its cause precisely and is written to the
    log for that reason, but it is the store's text, can carry tenant
    coordinates, and would become a declared part of this API's surface if it
    travelled on the error.
    """

    def __init__(self, source_path: str, operation: str, store_status: int | None = None) -> None:
        detail: dict = {"source_path": source_path, "operation": operation}
        if store_status is not None:
            detail["store_status"] = store_status
        super().__init__(
            "vault_source_store_refused",
            (
                f"The vault-source store refused to {operation} for "
                f"{source_path!r}. The refusal is not transient: resolve it at "
                f"the store before retrying."
            ),
            502,
            detail,
        )


class VaultSourceStoreUnavailableError(SAGEError):
    """503: the vault-source store declined to serve the operation just now.

    Throttling that outlasted the binding's one retry, a transient backend
    signal, an upload session the store expired or that was interrupted. The
    request was never judged on its merits, so the same one can succeed later
    unchanged -- which is the whole difference from
    :class:`VaultSourceStoreRefusedError`, and the reason the two are separate
    codes rather than one code with a flag: a caller reads the code to decide
    whether to retry or to escalate, and a flag inside a detail dict is easy to
    miss and easy to leave unread.

    Carries the same curated message discipline as its non-transient
    counterpart: the store's own body goes to the log, not to the caller.
    """

    def __init__(self, source_path: str, operation: str, store_status: int | None = None) -> None:
        detail: dict = {"source_path": source_path, "operation": operation}
        if store_status is not None:
            detail["store_status"] = store_status
        super().__init__(
            "vault_source_store_unavailable",
            (
                f"The vault-source store could not {operation} for "
                f"{source_path!r} just now. The same request may succeed on a "
                f"later attempt."
            ),
            503,
            detail,
        )


class RestoreSourceNotAbsoluteError(SAGEError):
    """400: the restore source path is not absolute.

    A restore reads bytes from the *caller's* filesystem, so the path names a
    file there and must say so unambiguously. A relative path has no defined
    meaning on this operation -- unlike an ingest, where it addresses a source
    already inside the vault -- and would otherwise resolve against the server
    process's working directory, which on a deployed profile is the container's,
    not the caller's. Refusing it also keeps the caller-local delivery gate
    honest: that gate triggers on an absolute path, so a relative one would slip
    past it and be read server-side instead of prompting an upload.
    """

    def __init__(self, source: str) -> None:
        super().__init__(
            "restore_source_not_absolute",
            f"Restore source must be an absolute path: {source}",
            400,
            {"source": source},
        )


class PathTraversalDeniedError(SAGEError):
    """400: output_path resolves outside vault storage_root (BH-038, BH-040)."""

    def __init__(self, output_path: str) -> None:
        super().__init__(
            "path_traversal_denied",
            f"Path resolves outside vault storage root: {output_path}",
            400,
            {"output_path": output_path},
        )


class NoProjectionError(SAGEError):
    """404: document has no stored projection."""

    def __init__(self, document_id: str) -> None:
        super().__init__(
            "no_projection",
            f"No projection stored for document {document_id}",
            404,
            {"document_id": document_id},
        )


class AssertionsFileNotFoundError(SAGEError):
    """404: retrieval health assertions file not found (BH-042).

    The vault config references an ``assertions_file`` that does not exist
    under the vault's ``storage_root`` -- a missing resource, so 404, distinct
    from ``AssertionsNotConfiguredError`` (no file configured at all, a 400
    precondition).
    """

    def __init__(self, path: str) -> None:
        super().__init__(
            "assertions_file_not_found",
            f"Assertions file not found: {path}",
            404,
            {"assertions_file": path},
        )


class AssertionsFileInvalidError(SAGEError):
    """400: retrieval health assertions file is malformed (BH-042)."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(
            "assertions_file_invalid",
            f"Assertions file invalid: {reason}",
            400,
            {"assertions_file": path, "reason": reason},
        )


class AssertionsNotConfiguredError(SAGEError):
    """400: no assertions_file configured in vault config."""

    def __init__(self) -> None:
        super().__init__(
            "assertions_not_configured",
            "No retrieval_health.assertions_file configured for this vault",
            400,
        )


class VaultNotFoundError(SAGEError):
    """404: vault_id does not match loaded config."""

    def __init__(self, vault_id: str) -> None:
        super().__init__(
            "vault_not_found",
            f"Vault '{vault_id}' not found",
            404,
            {"vault_id": vault_id},
        )


class VaultConfigValidationError(SAGEError):
    """400: vault config failed Pydantic validation."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__(
            "vault_config_validation_error",
            "Vault configuration is invalid",
            400,
            {"errors": errors},
        )


class VaultAlreadyExistsError(SAGEError):
    """409: vault_id already registered."""

    def __init__(self, vault_id: str) -> None:
        super().__init__(
            "vault_already_exists",
            f"Vault '{vault_id}' already exists",
            409,
            {"vault_id": vault_id},
        )


class ReabstractAlreadyInFlightError(SAGEError):
    """409: a reabstract_deferred operation is already running on the vault.

    Single-flight per vault: a second concurrent caller receives this
    structured error with the start_time of the running operation rather
    than queueing. The non-blocking rejection (vs. await lock.acquire())
    is intentional -- reabstract passes can run for minutes against the
    in-process Qwen3 provider, and silently queuing a second long-running
    caller would mask client-side coordination bugs.
    """

    def __init__(self, vault_id: str, start_time: datetime) -> None:
        super().__init__(
            "reabstract_already_in_flight",
            (
                f"A reabstract_deferred operation is already running on vault "
                f"{vault_id!r}; started at {start_time.isoformat()}."
            ),
            409,
            {"vault_id": vault_id, "start_time": start_time.isoformat()},
        )


class ReabstractDocumentAlreadyInFlightError(SAGEError):
    """409: a per-document reabstract is already running for this document_id.

    Single-flight per document: a second concurrent caller against the same
    document receives this structured error with the start_time of the
    running operation rather than dispatching a parallel background task.
    Concurrent reabstract calls against different document_ids in the
    same vault continue to run in parallel.
    """

    def __init__(self, document_id: str, start_time: datetime) -> None:
        super().__init__(
            "reabstract_document_already_in_flight",
            (
                f"A reabstract is already running on document "
                f"{document_id!r}; started at {start_time.isoformat()}."
            ),
            409,
            {"document_id": document_id, "start_time": start_time.isoformat()},
        )


class RecomputePipelineAlreadyInFlightError(SAGEError):
    """409: a per-document recompute_pipeline is already running for this document_id.

    Single-flight per document: a second concurrent caller against the same
    document receives this structured error with the start_time of the
    running operation rather than dispatching a parallel background task.
    Concurrent calls against different document_ids in the same vault
    continue to run in parallel.
    """

    def __init__(self, document_id: str, start_time: datetime) -> None:
        super().__init__(
            "recompute_pipeline_already_in_flight",
            (
                f"A recompute_pipeline is already running on document "
                f"{document_id!r}; started at {start_time.isoformat()}."
            ),
            409,
            {"document_id": document_id, "start_time": start_time.isoformat()},
        )


class DestructiveConfigChangeError(SAGEError):
    """409: vault config update would orphan existing documents.

    Raised when the merged config removes a doc_type or lifecycle state
    that still has documents attached, and the caller has not passed
    force=True (MCP) or ?force=true (REST).
    """

    def __init__(self, warnings: list[str]) -> None:
        super().__init__(
            "destructive_config_change",
            (
                "Vault configuration update would orphan existing documents. "
                "Pass force=true to proceed."
            ),
            409,
            {"warnings": warnings},
        )


class EdgeNotFoundError(SAGEError):
    """404: production edge not found."""

    def __init__(self, edge_id: str) -> None:
        super().__init__(
            "edge_not_found",
            f"Edge '{edge_id}' not found",
            404,
            {"edge_id": edge_id},
        )


class StagingEdgeNotFoundError(SAGEError):
    """404: staging edge not found (already confirmed/dismissed or never existed)."""

    def __init__(self, edge_id: str) -> None:
        super().__init__(
            "staging_edge_not_found",
            f"Staging edge '{edge_id}' not found",
            404,
            {"edge_id": edge_id},
        )


class ContentTooLargeError(SAGEError):
    """413: document file exceeds the inline content size ceiling (BH-118)."""

    def __init__(self, document_id: str, size_bytes: int, max_bytes: int) -> None:
        super().__init__(
            "content_too_large",
            (
                f"Document {document_id} file size {size_bytes} bytes exceeds "
                f"the inline content ceiling of {max_bytes} bytes"
            ),
            413,
            {
                "document_id": document_id,
                "size_bytes": size_bytes,
                "max_bytes": max_bytes,
            },
        )


class ContentFileMissingError(SAGEError):
    """404: document record exists but the vault-local file is absent (BH-119)."""

    def __init__(self, document_id: str, source_path: str) -> None:
        super().__init__(
            "content_file_missing",
            (f"Document {document_id} file is missing at vault-relative path {source_path}"),
            404,
            {"document_id": document_id, "source_path": source_path},
        )


class SupersedeTargetNotActiveError(SAGEError):
    """409: supersede is not legal from the target's state (BH-122).

    `allowed_states` comes from the vault's lifecycle transition table --
    the states that declare a `supersede` transition -- so the reported
    precondition tracks the vault's configuration instead of restating
    it. The error's name reflects a single-state rule that no table is
    obliged to hold: the create-vault scaffold declares `supersede` from
    `active` and from `completed`, so a vault built from it reports both
    rather than being rejected against a rule it does not hold. Read
    `allowed_states`, never the name. A vault whose table permits
    `supersede` from
    no state at all reports the empty set as such: substituting a state
    the vault does not permit would misreport the precondition.

    `required_state` renders the same set for humans through the shared
    state-set renderer, so it reads identically to every other
    config-derived precondition, and remains in the detail payload for
    callers that key remediation prose off it.
    """

    def __init__(
        self,
        predecessor_id: str,
        current_state: str,
        allowed_states: list[str] | None = None,
    ) -> None:
        states = sorted(allowed_states or [])
        required_state = render_state_set(states)
        super().__init__(
            "supersede_target_not_active",
            (
                f"Cannot supersede document {predecessor_id}: current state "
                f"'{current_state}', required '{required_state}'"
            ),
            409,
            {
                "predecessor_id": predecessor_id,
                "current_state": current_state,
                "required_state": required_state,
                "allowed_states": states,
            },
        )


class WritePathExistsError(SAGEError):
    """409: write_to_path target already exists (BH-126)."""

    def __init__(self, write_to_path: str) -> None:
        super().__init__(
            "write_path_exists",
            f"Target path already exists: {write_to_path}",
            409,
            {"write_to_path": write_to_path},
        )


class WritePathInvalidError(SAGEError):
    """400: write_to_path parent missing, not writable, or path not absolute (BH-127)."""

    def __init__(self, write_to_path: str, reason: str) -> None:
        super().__init__(
            "write_path_invalid",
            f"Cannot write to {write_to_path}: {reason}",
            400,
            {"write_to_path": write_to_path, "reason": reason},
        )


class ContentDeliveryConflictError(SAGEError):
    """400: caller set both include_content and write_to_path (BH-128)."""

    def __init__(self) -> None:
        super().__init__(
            "content_delivery_conflict",
            "include_content and write_to_path are mutually exclusive",
            400,
        )


class BinaryContentRefusedError(SAGEError):
    """400: include_content was requested against a binary-container source.

    A document whose source adapter is a binary container (``.docx``,
    ``.pptx``, ``.pdf``, ``.xlsx``) holds raw container bytes, not scannable text. The
    read path declines to inline those bytes so a caller cannot scan a
    binary container as though it were text and read a confident false
    negative. The readable content lives in the extracted-text projection;
    the detail directs the caller to ``read_projection`` (CAS-ADR-039).
    Distinct from ``document_not_found``: the document exists and is
    readable, only not via raw-byte delivery.
    """

    def __init__(self, document_id: str, source_type: str) -> None:
        super().__init__(
            "binary_content_refused",
            (
                f"Refusing to inline raw bytes for document {document_id}: "
                f"source type {source_type!r} is a binary container, not "
                f"scannable text. Use read_projection for the extracted text."
            ),
            400,
            {
                "document_id": document_id,
                "source_type": source_type,
                "use_instead": "read_projection",
            },
        )


class LocalOpenNotAvailableError(SAGEError):
    """501: the host OS opener is a local-profile affordance, gated off under cloud.

    ``POST /documents/{id}/open`` shells out the host OS opener
    (``open``/``xdg-open``/``startfile``), which is meaningful only when the
    browser and SAGE share a machine (the local profile). Under the cloud profile
    SAGE is a headless container with no desktop, so the opener is gated off and a
    caller delivers a document to the browser through a store-issued download URL
    (``GET /documents/{id}/download-url``) instead. Mirrors the
    "capability unavailable in this deployment" shape the co-located-only surfaces
    use.
    """

    def __init__(self) -> None:
        super().__init__(
            "local_open_only",
            (
                "The host OS opener is a local-profile affordance and is not "
                "available under the cloud profile; request a download URL instead."
            ),
            501,
        )


class DownloadUrlNotAvailableError(SAGEError):
    """501: the active vault-source binding cannot issue a source download URL.

    A store-issued download URL is a richer-binding capability (CAS-ADR-043): the
    document-store binding backs it with a short-lived pre-authenticated URL, while
    the filesystem binding has no equivalent. When the active binding lacks the
    capability the request is refused with this structured 501 rather than a bare
    failure, so a caller learns the deployment does not offer browser delivery for
    this document.
    """

    def __init__(self, document_id: str) -> None:
        super().__init__(
            "download_url_unavailable",
            (
                f"The active vault-source binding cannot issue a download URL for "
                f"document {document_id}."
            ),
            501,
            {"document_id": document_id},
        )


class CallerFilesystemUnavailableError(SAGEError):
    """501: a caller-supplied local path cannot be honored under this deployment.

    Under the cloud profile SAGE runs as a remote container that cannot see
    the calling client's filesystem. The byte-moving path tools answer with a
    transfer recipe instead, but ``list_directory`` has no byte leg to hand
    off -- walking and content-hashing a directory only makes sense against
    the caller's own tree -- so it is refused with this structured error
    naming the caller-side alternative, closing the container-walk disclosure.
    Mirrors the "capability unavailable in this deployment" shape of
    :class:`LocalOpenNotAvailableError` and
    :class:`DownloadUrlNotAvailableError` (CAS-ADR-042 constraint 1: the
    caller-visible surface stays profile-invariant; the per-profile byte
    transport below it is a binding detail).
    """

    def __init__(self, operation: str, remedy: str) -> None:
        super().__init__(
            "caller_filesystem_unavailable",
            (
                f"{operation} resolves a path on the SAGE server, which cannot "
                f"see the caller's filesystem under the cloud profile; {remedy}."
            ),
            501,
            {"operation": operation, "remedy": remedy},
        )


class TransferTokenInvalidError(SAGEError):
    """410: a transfer token does not name a redeemable pending transfer.

    Transfer tokens are short-lived, one-time, direction-scoped credentials
    minted by an authenticated call and redeemed against the transfer
    endpoints. One code covers every unredeemable state -- unknown, expired,
    already consumed, wrong direction, or wrong vault -- so the error surface
    is not an oracle distinguishing a token that never existed from one that
    just expired. The remedy is always the same: re-issue the originating
    call to mint a fresh token.
    """

    def __init__(self) -> None:
        super().__init__(
            "transfer_token_invalid",
            (
                "The transfer token does not name a redeemable pending "
                "transfer (unknown, expired, already used, or scoped to a "
                "different direction or vault); re-issue the originating "
                "call to mint a fresh token."
            ),
            410,
        )


class TransferNotStagedError(SAGEError):
    """409: an upload's completion call arrived before its bytes.

    Completing a caller-local ingest is a two-step exchange: the caller's
    environment first delivers the bytes to the upload endpoint, then the
    completion call redeems the token against the staged bytes. Arriving
    here without a successful upload leg is an ordering error, not a token
    error -- the token stays valid, and the remedy is to run the upload leg.
    """

    def __init__(self, transfer_id: str) -> None:
        super().__init__(
            "transfer_not_staged",
            (
                f"Transfer {transfer_id} has no staged bytes yet; deliver the "
                f"file to the upload endpoint with the transfer token, then "
                f"repeat this call."
            ),
            409,
            {"transfer_id": transfer_id},
        )


class TransferAlreadyStagedError(SAGEError):
    """409: a second upload attempted against an already-staged transfer.

    Each upload token admits exactly one successful byte delivery; the staged
    bytes then wait for the completion call. A repeat delivery is refused
    rather than silently overwriting the staged bytes, so a duplicated curl
    cannot race the completion.
    """

    def __init__(self, transfer_id: str) -> None:
        super().__init__(
            "transfer_token_already_used",
            (
                f"Transfer {transfer_id} already holds staged bytes; complete "
                f"the originating call, or mint a fresh token to re-send."
            ),
            409,
            {"transfer_id": transfer_id},
        )


class TransferContentTooLargeError(SAGEError):
    """413: an upload exceeded the transfer byte ceiling mid-stream.

    The upload endpoint bounds the body while streaming it to staging, so an
    oversize payload is aborted at the ceiling instead of filling the
    container's disk; the partial staging file is removed and the token
    reverts to retryable. The ceiling is ``SAGE_MAX_TRANSFER_BYTES``
    (default 100 MB).
    """

    def __init__(self, max_bytes: int) -> None:
        super().__init__(
            "transfer_content_too_large",
            (
                f"Upload exceeded the transfer ceiling of {max_bytes} bytes "
                f"and was aborted; send a smaller file or raise "
                f"SAGE_MAX_TRANSFER_BYTES."
            ),
            413,
            {"max_bytes": max_bytes},
        )


class TransferEndpointNotConfiguredError(SAGEError):
    """500: this deployment cannot mint transfer recipes.

    Minting a recipe requires the stack config to declare the public base URL
    the caller's environment can reach (``transfer.public_base_url``). A
    deployment that needs the caller-local byte channel but does not declare
    it fails loud at mint time with this structured error, rather than
    emitting a recipe whose URL cannot work.
    """

    def __init__(self) -> None:
        super().__init__(
            "transfer_endpoint_not_configured",
            (
                "This deployment does not declare transfer.public_base_url in "
                "its stack config, so no transfer recipe can be minted."
            ),
            500,
        )


class AmbiguousIngestSourceError(SAGEError):
    """400: both a path ``source`` and a ``transfer_token`` were supplied.

    The two are mutually exclusive delivery shapes for the same logical source:
    a ``source`` path, or a ``transfer_token`` redeeming bytes already
    delivered to the upload endpoint. Supplying both is refused so the caller
    learns which to drop, mirroring the exactly-one-of contract of
    :class:`AmbiguousDocumentIdentifierError`.
    """

    def __init__(self) -> None:
        super().__init__(
            "ambiguous_ingest_source",
            (
                "Supply exactly one of `source` (a source file path) or "
                "`transfer_token` (redeeming an already-delivered upload); "
                "both were provided."
            ),
            400,
        )


class MissingIngestSourceError(SAGEError):
    """400: neither a path ``source`` nor a ``transfer_token`` was supplied.

    Companion to :class:`AmbiguousIngestSourceError`: a document to ingest must
    arrive by exactly one of the two delivery shapes, and neither was provided.
    """

    def __init__(self) -> None:
        super().__init__(
            "missing_ingest_source",
            (
                "Supply exactly one of `source` (a source file path) or "
                "`transfer_token` (redeeming an already-delivered upload); "
                "neither was provided."
            ),
            400,
        )


class DeliveryParameterConflictError(SAGEError):
    """400: an explicit ``delivery`` mode contradicts the write_to_path argument.

    Raised by content read tools that accept a ``delivery`` selector
    (``inline | spill | auto``) when the requested mode cannot be honored:
    ``delivery="inline"`` was combined with a ``write_to_path`` target, or
    ``delivery="spill"`` was requested without one. The contradiction is
    refused rather than silently resolved so the caller learns which of the
    two arguments to drop.
    """

    def __init__(self, delivery: str, reason: str) -> None:
        super().__init__(
            "delivery_conflict",
            f"delivery={delivery!r} conflicts with write_to_path: {reason}",
            400,
            {"delivery": delivery, "reason": reason},
        )


class EdgeAnchorPolicyViolationError(SAGEError):
    """400: edge violates the resolution-policy write-time invariant (CAS-ADR-017).

    The invariant matrix is policy-keyed:
      - none (non-retracts): all anchor fields null, target_id required.
      - retracts: target_id null, retracted_edge_id required, source-side anchor only.
      - transitive_source: source-side anchor required; no target-side anchor.
      - transitive_both: both anchors required.
    """

    def __init__(
        self,
        edge_type: str,
        resolution_policy: str,
        violation: str,
        offending_fields: list[str] | None = None,
    ) -> None:
        detail: dict = {
            "edge_type": edge_type,
            "resolution_policy": resolution_policy,
            "violation": violation,
        }
        if offending_fields is not None:
            detail["offending_fields"] = offending_fields
        super().__init__(
            "edge_anchor_policy_violation",
            f"Edge violates resolution_policy '{resolution_policy}' invariant: {violation}",
            400,
            detail,
        )


class TBDPolicyEdgeError(SAGEError):
    """400: attempted to create an edge whose registry policy is TBD (CAS-ADR-017)."""

    def __init__(self, edge_type: str) -> None:
        super().__init__(
            "tbd_policy_edge",
            (
                f"Cannot create edge of type '{edge_type}': its resolution_policy "
                "is TBD. Freeze the policy in the edge_type_registry before use."
            ),
            400,
            {"edge_type": edge_type},
        )


class RetractTargetNotEdgeError(SAGEError):
    """400: retracts edge references an unknown edge id (CAS-ADR-017, Chunk 5)."""

    def __init__(self, retracted_edge_id: str) -> None:
        super().__init__(
            "retract_target_not_edge",
            (
                f"retracts edge references edge id '{retracted_edge_id}' that "
                "does not exist in the edges table"
            ),
            400,
            {"retracted_edge_id": retracted_edge_id},
        )


class MergedFromValidationError(SAGEError):
    """400: merged_from edge violates chain-position invariants (CAS-ADR-017, Chunk 6).

    The source (successor) must be the first version of its chain (no
    outbound supersedes edges from it) and the target (predecessor) must
    be the chain head (no supersedes edge points at it).
    """

    def __init__(
        self,
        violation: str,
        source_id: str | None = None,
        target_id: str | None = None,
    ) -> None:
        detail: dict = {"violation": violation}
        if source_id is not None:
            detail["source_id"] = source_id
        if target_id is not None:
            detail["target_id"] = target_id
        super().__init__(
            "merged_from_validation",
            f"merged_from edge invalid: {violation}",
            400,
            detail,
        )


class IdenticalContentSupersedeError(SAGEError):
    """409: attempted supersede whose content matches the predecessor (BH-123)."""

    def __init__(self, predecessor_id: str, source_content_hash: str) -> None:
        super().__init__(
            "identical_content_supersede",
            (
                f"Cannot supersede document {predecessor_id}: new content is "
                "identical to the predecessor (no-op edit)"
            ),
            409,
            {
                "predecessor_id": predecessor_id,
                "source_content_hash": source_content_hash,
            },
        )


class SyncedFromInapplicableEdgeType(SAGEError):
    """400: synced_from_* fields set on an edge_type other than sync_target /
    derived_from.

    The `synced_from_version` and `synced_from_content_hash` columns are
    semantically meaningful only on `sync_target` (Tier 1) and
    `derived_from` (Tier 3) edges. Setting them on any other edge type
    creates orphaned provenance the drift detector would never inspect.
    """

    def __init__(self, edge_type: str, fields_set: list[str]) -> None:
        super().__init__(
            "synced_from_inapplicable_edge_type",
            (
                f"synced_from_* fields {sorted(fields_set)!r} are not "
                f"applicable to edge_type '{edge_type}'; only 'sync_target' "
                "and 'derived_from' carry synced-from provenance."
            ),
            400,
            {"edge_type": edge_type, "fields_set": sorted(fields_set)},
        )


class SyncedFromVersionNotInSourceChain(SAGEError):
    """400: synced_from_version doc id is not a member of the target's
    supersedes chain.

    Raised when `create_edge` is called with a `synced_from_version` that
    either references a document outside the chain rooted at `target_id`
    or references a document id that does not resolve at all. Surfaced
    as this dedicated code (not `document_not_found`) so operators can
    distinguish "wrong document" from "missing document" — the
    remediation differs.
    """

    def __init__(self, target_id: str, synced_from_version: str) -> None:
        super().__init__(
            "synced_from_version_not_in_source_chain",
            (
                f"synced_from_version {synced_from_version!r} is not a member "
                f"of the supersedes chain rooted at target_id {target_id!r}."
            ),
            400,
            {
                "target_id": target_id,
                "synced_from_version": synced_from_version,
            },
        )


class AmbiguousDocumentIdentifierError(SAGEError):
    """400: caller supplied both the canonical parameter and an alias
    for the same logical document identifier.

    Some MCP tools accept the canonical name ``document_id`` as an alias
    for a tool-specific name (e.g., ``traverse`` accepts both
    ``start_id`` and ``document_id``). Supplying both is treated as
    ambiguous — even when the values are equal — to keep the call-shape
    contract simple: exactly one must be supplied.
    """

    def __init__(self, tool: str, canonical: str, alias: str) -> None:
        super().__init__(
            "ambiguous_document_identifier",
            (f"{tool}: supply exactly one of {canonical!r} or {alias!r}; both were provided."),
            400,
            {
                "tool": tool,
                "supplied": [canonical, alias],
            },
        )


class MissingDocumentIdentifierError(SAGEError):
    """400: caller supplied neither the canonical parameter nor any
    accepted alias for the document identifier.

    Companion to:class:`AmbiguousDocumentIdentifierError`. Distinct
    from ``document_not_found`` (404) and from a downstream Pydantic
    ``ValidationError``: this code fires before the service layer is
    invoked, when no document identifier was supplied at all. The
    ``accepted`` detail enumerates every parameter name the tool will
    take so the caller learns the alias without trial-and-error.
    """

    def __init__(self, tool: str, accepted: list[str]) -> None:
        super().__init__(
            "missing_document_identifier",
            (f"{tool}: a document identifier is required. Supply exactly one of: {accepted!r}."),
            400,
            {
                "tool": tool,
                "accepted": list(accepted),
            },
        )


# Filter keys whose accepted values are a closed Python enum. Drives the
# ``invalid_filter_value`` envelope, which reports the enum's members back
# to the caller. Vault-configured vocabularies are deliberately absent --
# their accepted set is not knowable at validation time.
_ENUM_TYPED_FILTER_FIELDS: dict[str, type[StrEnum]] = {
    "source_type": SourceType,
    "edge_type": EdgeType,
}

# Field-annotation strings used in InvalidFilterShapeError detail.
# Kept as a small lookup rather than introspected from RetrievalFilters because
# Pydantic v2's stringified annotations for ``str | None`` shapes are noisy
# (``typing.Optional[str]`` or ``Union[str, None]`` depending on Python form).
# Enum-typed keys are absent: a bad value on those raises ``enum`` rather
# than a ``*_type`` shape error, so it never reaches this table.
#
# A key whose field carries a typed alias names the alias, not the bare type
# it refines. Naming the bare type would make the envelope's own remedy
# unreachable: a caller told to supply ``list[str]`` who then supplies one
# with an off-shape entry gets a second, different 400 from the alias.
_FILTER_FIELD_TYPE_NAMES: dict[str, str] = {
    "doc_type": "str",
    "project": "str",
    "lifecycle_status": "str",
    "tags": "list[str]",
    "document_ids": "list[DocumentIdStr]",
    "pipeline_status": "str",
    "tier3_metadata": "dict",
}

# Remedies attached to ``invalid_parameter`` envelopes, keyed by the final
# segment of the failing location. Deliberately sparse: a hint earns its
# place by naming a way forward the constraint alone does not imply, and an
# absent entry yields an envelope with no ``hint`` key rather than filler.
# The bound itself is never restated here -- it comes from the validator's
# own message, so a changed cap cannot leave a stale number behind.
_PARAMETER_HINTS: dict[str, str] = {
    "limit": "Page through larger result sets with `offset`.",
}

# Request components FastAPI prepends to a validation error's location to
# name where the value came from. They are part of the framing, not part of
# the parameter path, so they are stripped before any location is matched
# against a rule or reported back to a caller.
_TRANSPORT_LOC_SEGMENTS = ("body", "query", "path", "header", "cookie")

# Types whose ``input`` a caller can be shown verbatim. Anything else is
# rendered with ``str`` so the envelope stays JSON-serializable on both
# transports regardless of what the caller supplied.
_JSON_NATIVE_TYPES = (str, int, float, bool, type(None))


def _strip_transport_segment(loc: tuple) -> tuple:
    """Drop a leading FastAPI request-component segment from a location."""
    if loc and loc[0] in _TRANSPORT_LOC_SEGMENTS:
        return loc[1:]
    return loc


def translate_validation_error(
    exc: ValidationError | RequestValidationError,
) -> SAGEError | None:
    """Map a Pydantic ValidationError on DiscoverRequest/RetrievalFilters to a
    typed ADR-028 SAGEError.

    Walks ``exc.errors()`` and returns the first matching SAGEError. Returns
    ``None`` when no rule matches, signaling the caller to fall back to the
    default validation-error path (FastAPI's 422 on HTTP, ``internal_error``
    on MCP). The translator is intentionally scoped: it only fires on
    ``mode``/``filters``-rooted errors so non-discover endpoints are
    unaffected.

    Both ``pydantic.ValidationError`` and ``fastapi.exceptions.RequestValidationError``
    expose ``.errors()`` with the same dict shape, so one function serves
    both transports.
    """
    # Local import sidesteps any future circular-import risk: errors.py is
    # imported by routers/services that themselves import models/schemas.
    from sage.models.enums import RetrievalMode
    from sage.models.schemas import RetrievalFilters

    for err in exc.errors():
        loc = tuple(err.get("loc") or ())
        # FastAPI's RequestValidationError prepends a "body" / "query" /
        # "path" location segment naming the request component the value
        # came from. Pydantic's ValidationError does not. Strip a leading
        # transport-component segment so one set of loc rules works for
        # both call sites.
        loc = _strip_transport_segment(loc)
        err_type = err.get("type", "")
        input_value = err.get("input")
        ctx = err.get("ctx") or {}

        # 0) Custom ``mode_parameter_mismatch`` raised from the
        # DiscoverRequest model_validator via PydanticCustomError. The
        # validator lives in sage.models.schemas which cannot import
        # sage.api.errors (import-linter "Models are a leaf layer"
        # contract). We reconstruct the public-facing SAGEError here from
        # the embedded ctx.
        if err_type == "mode_parameter_mismatch":
            return ModeParameterMismatchError(
                mode=str(ctx.get("mode", "")),
                forbidden_param=str(ctx.get("forbidden_param", "")),
                allowed_modes=list(ctx.get("allowed_modes") or []),
            )

        # 0a) Custom ``legacy_form`` raised from the UpdateMetadataRequest
        # and BulkMetadataItem model_validators via PydanticCustomError.
        # Same leaf-layer-contract reasoning as ``mode_parameter_mismatch``:
        # the validator can't construct ``LegacyFormError`` itself, so it
        # embeds the envelope fields in ``ctx`` and we rebuild here. Drives
        # the structured ``legacy_form`` 400 envelope on the FastAPI surface
        # (CAS-ADR-028 ops-object patch grammar).
        if err_type == "legacy_form":
            return LegacyFormError(
                field=str(ctx.get("field", "")),
                received_type=str(ctx.get("received_type", "")),
                example=str(ctx.get("example", "")),
            )

        # 0b) Custom ``invalid_document_id`` raised from the DocumentIdStr
        # AfterValidator via PydanticCustomError. Same leaf-layer-contract
        # reasoning as above: the validator embeds the offending value in
        # ``ctx`` and we rebuild the public 400 envelope here. Reconciles the
        # reject-at-boundary rule (a malformed id is a client error caught
        # before lookup) with the self-describing not-found discriminator (a
        # well-formed-but-absent id is a 404): malformed syntax surfaces as
        # ``invalid_document_id`` (400), never as the generic internal_error.
        if err_type == "invalid_document_id":
            return InvalidDocumentIdError(str(ctx.get("document_id", input_value)))

        # 0c) The rest of the typed-alias family: vault_id, edge_id, sha256,
        # function_id, document_date, user_id. Same leaf-layer-contract
        # reasoning as ``invalid_document_id`` above -- the validator embeds a
        # uniform ``{argument, value, expected}`` ctx and we rebuild the public
        # 400 here via the single parameterized ``InvalidTypedAliasError``.
        # Placed AFTER the ``invalid_document_id`` branch, which keeps its own
        # distinct error and three-key ctx; the ordering matters because
        # ``invalid_document_id`` is itself a member of the family set.
        if err_type in _TYPED_ALIAS_CODES:
            return InvalidTypedAliasError(
                code=err_type,
                argument=str(ctx.get("argument", "")),
                value=ctx.get("value", input_value),
                expected=str(ctx.get("expected", "")),
            )

        # 1) Invalid `mode` enum value: caller passed a string not in RetrievalMode.
        if loc and loc[0] == "mode" and err_type in ("enum", "literal_error"):
            valid_modes = [m.value for m in RetrievalMode]
            return InvalidModeError(mode=str(input_value), valid_modes=valid_modes)

        # 2) Unknown filter key: extra_forbidden under `filters`.
        if len(loc) >= 2 and loc[0] == "filters" and err_type == "extra_forbidden":
            key = str(loc[1])
            valid_keys = list(RetrievalFilters.model_fields.keys())
            example = (
                '{"tier3_metadata": {"ticket_id": "<id>"}} for typed '
                'metadata, or {"doc_type": "ticket"} for built-in fields'
            )
            return UnknownFilterKeyError(key=key, valid_keys=valid_keys, example=example)

        # 2a) Out-of-vocabulary value for an enum-typed filter key.
        # Pydantic reports these as `enum` (StrEnum members) or
        # `literal_error`, neither of which the shape branch below
        # catches -- it keys on the `*_type` suffix. Without this branch
        # such a value falls through untranslated to the generic 422/
        # internal_error path, losing the valid set the caller needs.
        # Scoped to the field->enum map rather than to any one field, so
        # every enum-typed filter key gets the same envelope.
        if len(loc) >= 2 and loc[0] == "filters" and err_type in ("enum", "literal_error"):
            field = str(loc[1])
            enum_cls = _ENUM_TYPED_FILTER_FIELDS.get(field)
            if enum_cls is not None:
                return InvalidFilterValueError(
                    field=field,
                    value=input_value,
                    valid_values=[member.value for member in enum_cls],
                )

        # 3) Wrong value type for a known filter key.
        # Pydantic emits types like `list_type`, `int_type`, `string_type`,
        # `dict_type` for primitive-shape failures.
        if len(loc) >= 2 and loc[0] == "filters" and err_type.endswith("_type"):
            field = str(loc[1])
            expected_type = _FILTER_FIELD_TYPE_NAMES.get(field, "unknown")
            received_type = type(input_value).__name__
            return InvalidFilterShapeError(
                field=field,
                expected_type=expected_type,
                received_type=received_type,
            )

    return None


def _generic_parameter_error(
    exc: ValidationError | RequestValidationError,
) -> InvalidParameterError:
    """Build an `invalid_parameter` envelope from a validation error.

    Reads only the structured fields Pydantic exposes per error --
    ``loc``, ``input`` and ``msg``. The rendered form of a validation
    error additionally carries the model class name and a link to the
    validator's documentation site; neither is meaningful to a caller of
    this API, so neither is read here. That is a property of the
    construction, not of any filtering applied afterwards.

    The first error is reported. Callers who need a specific one of
    several failures to win -- as the argument-model boundary does for
    unknown parameters -- select it before reaching this function.
    """
    errors = exc.errors()
    if not errors:  # pragma: no cover -- pydantic always reports at least one
        return InvalidParameterError(
            parameter="request",
            value=None,
            constraint="Request failed validation",
        )

    err = errors[0]
    loc = _strip_transport_segment(tuple(err.get("loc") or ()))
    parameter = ".".join(str(segment) for segment in loc) or "request"
    value = err.get("input")
    if not isinstance(value, _JSON_NATIVE_TYPES):
        value = str(value)

    return InvalidParameterError(
        parameter=parameter,
        value=value,
        constraint=str(err.get("msg", "Invalid value")),
        hint=_PARAMETER_HINTS.get(str(loc[-1]) if loc else ""),
    )


def validation_error_envelope(
    exc: ValidationError | RequestValidationError,
) -> SAGEError:
    """Map any validation error to a structured envelope (CAS-ADR-028).

    `translate_validation_error` handles the cases with a more specific
    code and returns ``None`` for the rest; this wrapper supplies the
    general `invalid_parameter` envelope for that remainder, so no
    validation failure reaches a caller as a raw Pydantic rendering.

    The division of labour is deliberate. The translator stays scoped to
    the rules it can state precisely, and remains usable by callers that
    need to know whether a specific rule matched. Uniformity is a property
    of this wrapper: every failure reaches *an* envelope, not the same one.
    """
    return translate_validation_error(exc) or _generic_parameter_error(exc)


def register_exception_handlers(app: FastAPI) -> None:
    """Register SAGE exception handlers on the FastAPI app."""

    @app.exception_handler(SAGEError)
    async def sage_error_handler(request: Request, exc: SAGEError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                code=exc.code,
                message=exc.message,
                detail=exc.detail,
            ).model_dump(exclude_none=True),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Translate request-validation failures into the SAGE envelope.

        Every failure reaches a structured envelope: the most specific
        code that applies, or the general `invalid_parameter` for the
        remainder. The generic envelope carries 422 -- the status request
        validation already returned -- so what changes on this path is the
        response body, not any endpoint's status code. Endpoints whose
        failures translate to a more specific code keep the 400 that code
        declares.
        """
        sage_err = validation_error_envelope(exc)
        return JSONResponse(
            status_code=sage_err.status_code,
            content=ErrorResponse(
                code=sage_err.code,
                message=sage_err.message,
                detail=sage_err.detail,
            ).model_dump(exclude_none=True),
        )
