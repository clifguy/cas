"""GET /sage_vaults/{vault_id}/documents/{document_id} -- get_document (BH-022).

Supports the agentic read-modify-reingest round-trip via two
mutually-exclusive content-delivery modes:
- `include_content=true` returns base64 bytes inline (BH-116 through BH-119).
- `write_to_path=<path>` writes bytes to disk and returns metadata
  (BH-125 through BH-128). Preferred for files that would otherwise
  exceed MCP tool-result size ceilings.
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from sage.api.dependencies import (
    get_documents_service,
    get_ingestion_service,
    get_vault_id,
)
from sage.models.schemas import (
    DocumentDownloadUrlResponse,
    DocumentIdStr,
    DocumentWithContent,
    ErrorResponse,
    OpenDocumentResponse,
    ReabstractStartedResponse,
    VaultIdStr,
)
from sage.services.documents import DocumentsService
from sage.services.ingestion import IngestionService

router = APIRouter(tags=["Document Metadata"])


@router.post(
    "/documents/{document_id}/open",
    response_model=OpenDocumentResponse,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "Document, vault, or backing source file not found.",
        },
        501: {
            "model": ErrorResponse,
            "description": (
                "`local_open_only`: the host OS opener is a local-profile "
                "affordance and is gated off under the cloud profile, where "
                "SAGE runs headless."
            ),
        },
    },
)
async def open_document(
    document_id: DocumentIdStr,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: DocumentsService = Depends(get_documents_service),
) -> OpenDocumentResponse:
    return await service.open_document_locally(document_id)


@router.post(
    "/documents/{document_id}/reabstract",
    operation_id="recompute_abstract",
    response_model=ReabstractStartedResponse,
    responses={
        404: {
            "model": ErrorResponse,
            "description": (
                "`document_not_found` / `no_projection` / `vault_not_found`: the "
                "document, its stored projection chunks, or the vault could not be "
                "resolved."
            ),
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "`reabstract_document_already_in_flight`: a re-abstraction is "
                "already running for this document."
            ),
        },
    },
)
async def recompute_abstract(
    document_id: DocumentIdStr,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: IngestionService = Depends(get_ingestion_service),
) -> ReabstractStartedResponse:
    """Regenerate the semantic abstract for a single document (fire-and-forget).

    Calls the shared per-document re-abstraction service path so this route and
    the equivalent MCP tool dispatch identical work against one queue and one
    single-flight claim. Returns as soon as the background job is enqueued; the
    caller waits on ``GET /documents/{document_id}`` for the transition out of
    ``abstraction_in_progress``, as a single bounded wait rather than one status
    read per unit of caller work. A concurrent call against the same document
    returns 409 rather than dispatching a parallel task. Regenerates regardless
    of the document's current terminal ``pipeline_status`` as long as its
    projection chunks are still stored.
    """
    result = await service.reabstract(document_id)
    return ReabstractStartedResponse(**result)


@router.get(
    "/documents/{document_id}/download-url",
    response_model=DocumentDownloadUrlResponse,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "Vault or document not found.",
        },
        501: {
            "model": ErrorResponse,
            "description": (
                "`download_url_unavailable`: the active vault-source binding "
                "cannot issue a download URL (the filesystem binding has no "
                "equivalent to the document-store binding's pre-authenticated "
                "URL)."
            ),
        },
        502: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_refused`: the vault-source store declined the "
                "operation on its merits -- quota, a permission it withdrew, a reply "
                "that could not be used. Resolve it at the store before retrying; "
                "`detail.store_status` carries the status it declined with."
            ),
        },
        503: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_unavailable`: the vault-source store declined to "
                "serve the operation just now -- throttling, or a transient backend "
                "signal. The same request may succeed on a later attempt."
            ),
        },
    },
)
async def get_document_download_url(
    document_id: DocumentIdStr,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: DocumentsService = Depends(get_documents_service),
) -> DocumentDownloadUrlResponse:
    return await service.get_document_download_url(document_id)


@router.get(
    "/documents/{document_id}/content",
    responses={
        404: {
            "model": ErrorResponse,
            "description": (
                "Vault or document not found. Also returned with "
                "`content_file_missing` when the retained source file is "
                "absent from the vault-source store."
            ),
        },
        502: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_refused`: the vault-source store declined the "
                "operation on its merits -- quota, a permission it withdrew, a reply "
                "that could not be used. Resolve it at the store before retrying; "
                "`detail.store_status` carries the status it declined with."
            ),
        },
        503: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_unavailable`: the vault-source store declined to "
                "serve the operation just now -- throttling, or a transient backend "
                "signal. The same request may succeed on a later attempt."
            ),
        },
    },
)
async def get_document_content(
    document_id: DocumentIdStr,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: DocumentsService = Depends(get_documents_service),
) -> StreamingResponse:
    """Stream a document's retained source bytes as a raw download.

    The binding-agnostic browser-delivery path (CAS-ADR-043): bytes come
    chunked from the vault-source store's streaming read, so no hop holds the
    whole file and the inline-content size ceiling does not apply.
    Binary-container sources are served raw with their correct media type --
    the CAS-ADR-039 refusal guards text-scanning of container bytes inlined
    into JSON, and this raw byte channel is the sanctioned alternative. The
    source's presence and size are resolved before the stream opens, so those
    failures arrive as structured JSON envelopes; a vault-source store refusal
    raised once the bytes are already flowing ends the body short of the
    promised Content-Length instead.
    """
    delivery = await service.get_document_content(document_id)
    filename = delivery.filename.replace("\\", "_").replace('"', "_")
    disposition = f'attachment; filename="{filename}"'
    if not filename.isascii():
        disposition = (
            f'attachment; filename="{filename.encode("ascii", "replace").decode()}"; '
            f"filename*=UTF-8''{quote(filename)}"
        )
    return StreamingResponse(
        delivery.chunks,
        media_type=delivery.media_type,
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(delivery.size),
        },
    )


@router.get(
    "/documents/{document_id}",
    response_model=DocumentWithContent,
    responses={
        400: {
            "model": ErrorResponse,
            "description": (
                "Request shape invalid: both `include_content` and "
                "`write_to_path` supplied (`content_delivery_conflict`), "
                "`write_to_path` is not absolute, or the parent directory of "
                "`write_to_path` is missing or not writable. Also returned with "
                "`binary_content_refused` when `include_content=true` targets a "
                "binary-container source (`.docx`, `.pptx`, `.pdf`, `.xlsx`): the read "
                "path declines to inline raw container bytes and directs the "
                "caller to `read_projection` for the extracted text "
                "(CAS-ADR-039)."
            ),
        },
        404: {
            "model": ErrorResponse,
            "description": (
                "Vault or document not found. Also returned when "
                "`include_content=true` or `write_to_path` is set and the "
                "file at `storage_root/source_path` is absent from the vault."
            ),
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "`write_to_path` target already exists. Callers must supply a "
                "fresh path or delete the prior file before retrying."
            ),
        },
        413: {
            "model": ErrorResponse,
            "description": (
                "`include_content=true` requested, but the file exceeds the "
                "configured inline content ceiling (default 100 MB; override "
                "via `SAGE_MAX_INLINE_CONTENT_BYTES`). Does not apply to "
                "`write_to_path` delivery. Use `write_to_path` or a "
                "filesystem-based workflow for files above the ceiling."
            ),
        },
        502: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_refused`: the vault-source store declined the "
                "operation on its merits -- quota, a permission it withdrew, a reply "
                "that could not be used. Resolve it at the store before retrying; "
                "`detail.store_status` carries the status it declined with."
            ),
        },
        503: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_unavailable`: the vault-source store declined to "
                "serve the operation just now -- throttling, or a transient backend "
                "signal. The same request may succeed on a later attempt."
            ),
        },
    },
)
async def get_document(
    document_id: DocumentIdStr,
    include_content: bool = Query(default=False),
    write_to_path: str | None = Query(default=None),
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: DocumentsService = Depends(get_documents_service),
) -> DocumentWithContent:
    return await service.get_document_with_content(document_id, include_content, write_to_path)
