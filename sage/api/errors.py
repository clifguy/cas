"""SAGE error hierarchy and FastAPI exception handlers.

Exception classes carry structured detail dicts matching the OpenAPI
ErrorResponse schema. The exception handler converts them to JSON responses.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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
    """400: tier3_metadata payload failed validation (T-0004).

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
