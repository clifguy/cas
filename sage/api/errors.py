"""SAGE error hierarchy and FastAPI exception handlers.

Exception classes carry structured detail dicts matching the OpenAPI
ErrorResponse schema. The exception handler converts them to JSON responses.
"""

from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

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
    def __init__(self, document_id: str) -> None:
        super().__init__(
            "document_not_found",
            f"Document {document_id} not found",
            404,
            {"document_id": document_id},
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


class InvalidModeError(SAGEError):
    """400: discover mode value is not in the RetrievalMode enum."""

    def __init__(self, mode: str, valid_modes: list[str]) -> None:
        super().__init__(
            "invalid_mode",
            f"Unknown discover mode: {mode!r}. Valid modes: {sorted(valid_modes)!r}",
            400,
            {"mode": mode, "valid_modes": sorted(valid_modes)},
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
    """400: retrieval health assertions file not found (BH-042)."""

    def __init__(self, path: str) -> None:
        super().__init__(
            "assertions_file_not_found",
            f"Assertions file not found: {path}",
            400,
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
    """409: supersede target is not in the `active` state (BH-122)."""

    def __init__(
        self, predecessor_id: str, current_state: str, required_state: str = "active"
    ) -> None:
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


# Field-annotation strings used in InvalidFilterShapeError detail.
# Kept as a small lookup rather than introspected from RetrievalFilters because
# Pydantic v2's stringified annotations for ``str | None`` shapes are noisy
# (``typing.Optional[str]`` or ``Union[str, None]`` depending on Python form).
_FILTER_FIELD_TYPE_NAMES: dict[str, str] = {
    "doc_type": "str",
    "project": "str",
    "lifecycle_status": "str",
    "tags": "list[str]",
    "document_ids": "list[str]",
    "pipeline_status": "str",
    "tier3_metadata": "dict",
}


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
        if loc and loc[0] in ("body", "query", "path", "header", "cookie"):
            loc = loc[1:]
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

        # 1) Invalid `mode` enum value: caller passed a string not in RetrievalMode.
        if loc and loc[0] == "mode" and err_type in ("enum", "literal_error"):
            valid_modes = [m.value for m in RetrievalMode]
            return InvalidModeError(mode=str(input_value), valid_modes=valid_modes)

        # 2) Unknown filter key: extra_forbidden under `filters`.
        if len(loc) >= 2 and loc[0] == "filters" and err_type == "extra_forbidden":
            key = str(loc[1])
            valid_keys = list(RetrievalFilters.model_fields.keys())
            example = (
                '{"tier3_metadata": {"ticket_id": "T-0001"}} for typed '
                'metadata, or {"doc_type": "ticket"} for built-in fields'
            )
            return UnknownFilterKeyError(key=key, valid_keys=valid_keys, example=example)

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
        """Translate FastAPI request-body validation errors into structured
        SAGE envelopes for the cases ``translate_validation_error``
        recognises (today: ADR-028 envelopes for the discover endpoint and
        the ``legacy_form`` envelope for ``update_metadata`` /
        ``bulk_update_metadata`` bare-list / bare-dict callers). Falls
        through to FastAPI's default 422 envelope for any unmatched
        validation error."""
        sage_err = translate_validation_error(exc)
        if sage_err is not None:
            return JSONResponse(
                status_code=sage_err.status_code,
                content=ErrorResponse(
                    code=sage_err.code,
                    message=sage_err.message,
                    detail=sage_err.detail,
                ).model_dump(exclude_none=True),
            )
        # Non-discover validation errors keep FastAPI's default 422 with its
        # native body shape. Returning a custom 422 envelope here would be a
        # larger blast radius than scopes; that's a follow-up.
        # ``jsonable_encoder`` handles ctx.error ValueError values that
        # ``json.dumps`` cannot serialize directly.
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(exc.errors())},
        )
