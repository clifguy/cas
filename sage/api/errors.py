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
        self, current_state: str, attempted_action: str, valid_actions: list[str]
    ) -> None:
        super().__init__(
            "invalid_lifecycle_transition",
            f"Cannot {attempted_action} from {current_state}",
            409,
            {
                "current_state": current_state,
                "attempted_action": attempted_action,
                "valid_actions": valid_actions,
            },
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
    """404: heading path not found in document (BH-030)."""

    def __init__(self, heading_path: str, document_id: str) -> None:
        super().__init__(
            "heading_not_found",
            f"Heading '{heading_path}' not found in document {document_id}",
            404,
            {"heading_path": heading_path, "document_id": document_id},
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
